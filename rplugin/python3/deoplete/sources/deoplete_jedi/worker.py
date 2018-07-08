import logging
import os
import queue
import sys
import threading
import time

from .server import Client
from .utils import file_mtime

log = logging.getLogger('deoplete.jedi.worker')
workers = []
work_queue = queue.Queue()
comp_queue = queue.Queue()


class Worker(threading.Thread):
    _exc_info = None
    """Exception info being set in threads."""
    daemon = True

    def __init__(self, python_path, in_queue, out_queue, desc_len=0,
                 server_timeout=10, short_types=False, show_docstring=False,
                 debug=False):
        self._client = Client(python_path, desc_len, short_types,
                              show_docstring, debug)
        self.server_timeout = server_timeout
        self.in_queue = in_queue
        self.out_queue = out_queue
        super(Worker, self).__init__()
        self.log = log.getChild(self.name)

    def completion_work(self, cache_key, extra_modules, source, line, col,
                        filename, options):
        try:
            completions = self._client.completions(cache_key, source, line, col,
                                                   filename, options)
        except Exception:
            self._exc_info = sys.exc_info()
            return
        modules = {f: file_mtime(f) for f in extra_modules}
        if completions is not None:
            for c in completions:
                m = c['module']
                if m and m not in modules and os.path.exists(m):
                    modules[m] = file_mtime(m)

        self.results = {
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

                self.results = None
                t = threading.Thread(target=self.completion_work, args=work)
                t.start()
                t.join(timeout=self.server_timeout)

                if self._exc_info is not None:
                    break

                if self.results:
                    self.out_queue.put(self.results)
                    self.log.debug('Completed work')
                else:
                    self.log.warn('Restarting server because it\'s taking '
                                  'too long')
                    # Kill all but the last queued job since they're most
                    # likely a backlog that are no longer relevant.
                    while self.in_queue.qsize() > 1:
                        self.in_queue.get()
                        self.in_queue.task_done()
                    self._client.restart()
                self.in_queue.task_done()
            except Exception:
                self.log.debug('Worker error', exc_info=True)

    def join(self):
        """Join the thread and raise any exception from it.

        This is used and picked up by :func:`Source._ensure_workers_are_alive`.
        """
        threading.Thread.join(self)
        if self._exc_info:
            raise self._exc_info[1]


def start(python_path, count, desc_len=0, server_timeout=10, short_types=False,
          show_docstring=False, debug=False):
    while count > 0:
        t = Worker(python_path, work_queue, comp_queue, desc_len,
                   server_timeout, short_types, show_docstring, debug)
        workers.append(t)
        t.start()
        log.debug('Started worker: %r', t)
        count -= 1
