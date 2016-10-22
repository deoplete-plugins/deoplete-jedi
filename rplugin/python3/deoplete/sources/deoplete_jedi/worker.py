import os
import time
import queue
import logging
import threading

from .server import Client
from .utils import file_mtime

log = logging.getLogger('deoplete.jedi.worker')
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
        self.log = log.getChild(self.name)

    def completion_work(self, cache_key, extra_modules, source, line, col,
                        filename, options):
        completions = self._client.completions(cache_key, source, line, col,
                                               filename, options)
        modules = {f: file_mtime(f) for f in extra_modules}
        if completions is not None:
            for c in completions:
                m = c['module']
                if m and m not in modules and os.path.exists(m):
                    modules[m] = file_mtime(m)

        return {
            'cache_key': cache_key,
            'time': time.time(),
            'modules': modules,
            'completions': completions,
        }

    def run(self):
        while True:
            try:
                work = self.in_queue.get()
                self.log.debug('Got work')
                self.out_queue.put(self.completion_work(*work), timeout=0.5)
                self.log.debug('Completed work')
            except Exception:
                self.log.debug('Worker error', exc_info=True)


def start(count, desc_len=0, short_types=False, show_docstring=False,
          debug=False, python_path=None):
    while count:
        t = Worker(work_queue, comp_queue, desc_len, short_types,
                   show_docstring, debug, python_path)
        workers.append(t)
        t.start()
        count -= 1
