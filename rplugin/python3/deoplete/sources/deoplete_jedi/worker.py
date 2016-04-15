import os
import json
import time
import queue
import logging
import threading

from .server import Client

log = logging.getLogger('deoplete.jedi')
workers = []
work_queue = queue.Queue()
comp_queue = queue.Queue()


class Worker(threading.Thread):
    daemon = True

    def __init__(self, in_queue, out_queue, desc_len=0,
                 short_types=False, show_docstring=False, debug=False):
        self._client = Client(desc_len, short_types, show_docstring, debug)
        self.in_queue = in_queue
        self.out_queue = out_queue
        super(Worker, self).__init__()
        self.log = logging.getLogger('deoplete.jedi.%s' % self.name)

        cache_dir = os.getenv('XDG_CACHE_HOME', '~/.cache')
        cache_version = '.'.join(str(x) for x in self._client.version[:2])
        self.cache_dir = os.path.join(os.path.expanduser(cache_dir),
                                      'deoplete/jedi', cache_version)
        if not os.path.exists(self.cache_dir):
            umask = os.umask(0)
            os.makedirs(self.cache_dir, 0o0700)
            os.umask(umask)

        # List of items loaded from the file system. `import~` is a special
        # key for caching import modules. It should not be cached to disk.
        self._loaded_cache = set(['import~'])

    def completion_work(self, cache_key, extra_modules, source, line, col,
                        filename):
        cached = None
        cache_file = None
        if len(cache_key) == 1 and cache_key[0] != 'import~':
            cache_file = os.path.join(self.cache_dir,
                                      '{}.json'.format(cache_key[0]))

        if cache_file and cache_key[0] not in self._loaded_cache:
            # The cache file is loaded only once.  The core cache will manage
            # the import cache and call this when there's a need to refresh.
            if os.path.isfile(cache_file):
                with open(cache_file, 'rt') as fp:
                    self._loaded_cache.add(cache_key[0])
                    try:
                        cached = json.load(fp)
                        cached['cache_key'] = cache_key
                        cached['time'] = time.time()
                    except Exception:
                        # If loading fails, fall through and allow the cache to
                        # be rewritten.
                        raise

        if cached:
            return cached

        completions = self._client.completions(cache_key, source, line, col,
                                               filename)
        out = None
        modules = {f: int(os.path.getmtime(f)) for f in extra_modules}
        if completions is not None:
            out = []
            for c in completions:
                module_path, name, type_, desc, abbr, kind = c
                if module_path and module_path not in modules \
                        and os.path.exists(module_path):
                    modules[module_path] = int(os.path.getmtime(module_path))

                out.append({
                    '$type': type_,
                    'word': name,
                    'abbr': abbr,
                    'kind': kind,
                    'info': desc,
                    'menu': '[jedi] ',
                    'dup': 1,
                })

        cached = {
            'cache_key': cache_key,
            'time': time.time(),
            'modules': modules,
            'completions': out,
        }

        if cache_file:
            # Should a lock file be used?
            with open(cache_file, 'wt') as fp:
                json.dump(cached, fp)

        return cached

    def run(self):
        while True:
            try:
                work = self.in_queue.get(block=False, timeout=0.5)
                self.log.debug('Got work')
                self.out_queue.put(self.completion_work(*work), block=False)
                self.log.debug('Completed work')
            except queue.Empty:
                # Sleep is mandatory to avoid pegging the CPU
                time.sleep(0.01)
            except Exception:
                self.log.debug('Worker error', exc_info=True)
                time.sleep(0.05)


def start(count, desc_len=0, short_types=False, show_docstring=False,
          debug=False):
    while count:
        t = Worker(work_queue, comp_queue, desc_len, short_types,
                   show_docstring, debug)
        workers.append(t)
        t.start()
        count -= 1
