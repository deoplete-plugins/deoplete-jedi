import os
import json
import time
import queue
import logging
import threading

from .server import Client
from .utils import file_mtime

log = logging.getLogger('deoplete.jedi')
workers = []
work_queue = queue.Queue()
comp_queue = queue.Queue()


class Worker(threading.Thread):
    daemon = True

    def __init__(self, in_queue, out_queue, desc_len=0,
                 short_types=False, show_docstring=False, debug=False,
                 python_path=None):
        self._client = Client(desc_len, short_types, show_docstring, debug,
                              python_path)

        self.in_queue = in_queue
        self.out_queue = out_queue
        super(Worker, self).__init__()
        self.log = logging.getLogger('deoplete.jedi.%s' % self.name)

    def completion_work(self, cache_key, extra_modules, source, line, col,
                        filename):
        completions = self._client.completions(cache_key, source, line, col,
                                               filename)
        out = None
        modules = {f: file_mtime(f) for f in extra_modules}
        if completions is not None:
            out = []
            for c in completions:
                module_path, name, type_, desc, abbr, kind = c
                if module_path and module_path not in modules \
                        and os.path.exists(module_path):
                    modules[module_path] = file_mtime(module_path)

                out.append({
                    '$type': type_,
                    'word': name,
                    'abbr': abbr,
                    'kind': kind,
                    'info': desc,
                    'menu': '[jedi] ',
                    'dup': 1,
                })

        return {
            'cache_key': cache_key,
            'time': time.time(),
            'modules': modules,
            'completions': out,
        }

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
          debug=False, python_path=None):
    while count:
        t = Worker(work_queue, comp_queue, desc_len, short_types,
                   show_docstring, debug, python_path)
        workers.append(t)
        t.start()
        count -= 1
