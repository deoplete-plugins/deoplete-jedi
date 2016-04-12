import os
import re
import sys
import time
import queue

sys.path.insert(1, os.path.dirname(__file__))

from deoplete.sources.base import Base
from deoplete_jedi import worker
from deoplete_jedi.cache import cache_context


_block_re = re.compile(r'^\s*(def|class)\s')


class Source(Base):

    def __init__(self, vim):
        Base.__init__(self, vim)
        self.cache = {}
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

    def indent_bounds(self, line, source):
        """Gets the bounds for a Python block.

        Only search for def or class for context.  Jedi only returns results
        from the lines before the current one.
        """
        start = line - 1
        indent = len(source[start]) - len(source[start].lstrip())

        for i in range(start, 0, -1):
            s_line = source[i]
            if not s_line.strip() or not _block_re.match(s_line):
                continue

            line_indent = len(s_line) - len(s_line.lstrip())
            if line_indent < indent:
                start = i
                indent = line_indent
                break

        return [start, line - 1]

    def process_result_queue(self):
        """Process completion results

        This should be called before new completions begin.
        """
        while True:
            try:
                cache_key, compl = worker.comp_queue.get(block=False,
                                                         timeout=0.05)
                cached = self.cache.get(cache_key)
                # Ensure that the incoming completion is actually newer than
                # the current one.
                if cached is None or cached.get('time') <= compl.get('time'):
                    self.cache[cache_key] = compl
            except queue.Empty:
                break

    def gather_candidates(self, context):
        if not self.workers_started:
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
        cache_line = 0
        cached = None
        refresh = True
        wait = False

        # Inclusion filters for the results
        filters = []

        if re.match('^\s*(from|import)\s+', context['input']) \
                and not re.match('^\s*from\s+\S+\s+', context['input']):
            # If starting an import, only show module results
            filters.append('module')

        cache_key, cache_line, extra_modules = cache_context(buf.name, context)

        if cache_key and cache_key in self.cache:
            # XXX: Hash cache keys to reduce length?
            cached = self.cache.get(cache_key)
            lines = cached.get('lines', [0, 0])
            modules = cached.get('modules')
            if cache_line >= lines[0] and cache_line <= lines[1] \
                    and all([filename in modules for filename in
                             extra_modules]) \
                    and all([int(os.path.getmtime(filename)) == mtime
                             for filename, mtime in modules.items()]):
                # The cache is still valid
                refresh = False

        if cached is None:
            wait = True

        self.debug('Key: %r, Refresh: %r, Wait: %r', cache_key, refresh, wait)
        if cache_key and (not cached or refresh):
            cache_lines = [0, 0]
            if cache_line:
                cache_lines = self.indent_bounds(line, src)
            n = time.time()
            worker.work_queue.put((cache_key, cache_lines, extra_modules,
                                   '\n'.join(src), line, col, str(buf.name)))
            while wait and time.time() - n < 1:
                self.process_result_queue()
                cached = self.cache.get(cache_key)
                if cached and cached.get('time') >= n:
                    break
                time.sleep(0.05)

        if cached:
            out = cached.get('completions')
            if filters:
                return [x for x in out if x['$type'] in filters]
            return out
        return []
