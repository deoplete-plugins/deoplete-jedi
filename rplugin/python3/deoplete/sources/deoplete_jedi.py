import os
import re
import sys
import time
import queue

sys.path.insert(1, os.path.dirname(__file__))

from deoplete.sources.base import Base
from deoplete_jedi import worker, profiler
from deoplete_jedi import cache


_block_re = re.compile(r'^\s*(def|class)\s')


class Source(Base):

    def __init__(self, vim):
        Base.__init__(self, vim)
        self.name = 'jedi'
        self.mark = '[jedi]'
        self.rank = 500
        self.filetypes = ['python']
        self.input_pattern = (r'[^. \t0-9]\.\w*$|'
                              r'^\s*@\w*$|'
                              r'^\s*from\s.+import \w*|'
                              r'^\s*from \w*|'
                              r'^\s*import \w*')

        self.debug_enabled = \
            self.vim.vars['deoplete#sources#jedi#debug_enabled']

        self.description_length = \
            self.vim.vars['deoplete#sources#jedi#statement_length']

        self.use_short_types = \
            self.vim.vars['deoplete#sources#jedi#short_types']

        self.show_docstring = \
            self.vim.vars['deoplete#sources#jedi#show_docstring']

        self.complete_min_length = \
            self.vim.vars['deoplete#auto_complete_start_length']

        self.worker_threads = \
            self.vim.vars['deoplete#sources#jedi#worker_threads']

        self.workers_started = False

    def get_complete_position(self, context):
        m = re.search(r'\w*$', context['input'])
        return m.start() if m else -1

    def process_result_queue(self):
        """Process completion results

        This should be called before new completions begin.
        """
        while True:
            try:
                compl = worker.comp_queue.get(block=False, timeout=0.05)
                cache_key = compl.get('cache_key')
                cached = cache.retrieve(cache_key)
                # Ensure that the incoming completion is actually newer than
                # the current one.
                if cached is None or cached.time <= compl.get('time'):
                    cache.store(cache_key, compl)
            except queue.Empty:
                break

    @profiler.profile
    def gather_candidates(self, context):
        if not self.workers_started:
            cache.start_reaper()
            worker.start(max(1, self.worker_threads), self.description_length,
                         self.use_short_types, self.show_docstring,
                         self.debug_enabled)
            self.workers_started = True

        self.process_result_queue()

        line = context['position'][1]
        col = context['complete_position']
        buf = self.vim.current.buffer
        src = buf[:]

        extra_modules = []
        cache_key = None
        cached = None
        refresh = True
        wait = False

        # Inclusion filters for the results
        filters = []

        if re.match('^\s*(from|import)\s+', context['input']) \
                and not re.match('^\s*from\s+\S+\s+', context['input']):
            # If starting an import, only show module results
            filters.append('module')

        cache_key, extra_modules = cache.cache_context(buf.name, context, src)

        if cache_key and cache.exists(cache_key):
            # XXX: Hash cache keys to reduce length?
            cached = cache.retrieve(cache_key)
            modules = cached.modules
            if all([filename in modules for filename in extra_modules]) \
                    and all([int(os.path.getmtime(filename)) == mtime
                             for filename, mtime in modules.items()]):
                # The cache is still valid
                refresh = False

        if cache_key and (cache_key[-1] == 'vars' or
                          (cached and len(cache_key) == 1 and
                           not len(cached.modules))):
            # Always refresh scoped variables
            refresh = True

        if cached is None:
            wait = True

        self.debug('Key: %r, Refresh: %r, Wait: %r', cache_key, refresh, wait)
        if cache_key and (not cached or refresh):
            n = time.time()
            worker.work_queue.put((cache_key, extra_modules, '\n'.join(src),
                                   line, col, str(buf.name)))
            while wait and time.time() - n < 1:
                self.process_result_queue()
                cached = cache.retrieve(cache_key)
                if cached and cached.time >= n:
                    break
                time.sleep(0.05)

        if cached:
            cached.touch()
            if filters:
                return [x for x in cached.completions if x['$type'] in filters]
            return cached.completions
        return []
