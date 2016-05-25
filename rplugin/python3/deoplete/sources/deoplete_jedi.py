import os
import re
import sys
import time

sys.path.insert(1, os.path.dirname(__file__))

from deoplete_jedi import cache, worker, profiler
from deoplete.sources.base import Base


def sort_key(item):
    w = item.get('word')
    l = len(w)
    z = l - len(w.lstrip('_'))
    return (('z' * z) + w.lower()[z:], len(w))


class Source(Base):

    def __init__(self, vim):
        Base.__init__(self, vim)
        self.name = 'jedi'
        self.mark = '[jedi]'
        self.rank = 500
        self.filetypes = ['python']
        self.input_pattern = (r'[\w\)\]\}\'\"]+\.\w*$|'
                              r'\w+\s*=\s*\w*$|'
                              r'^\s*@\w*$|'
                              r'^\s*from\s+[\w\.]*(?:\s+import\s+(?:\w*(?:,\s*)?)*)?|'
                              r'^\s*import\s+(?:[\w\.]*(?:,\s*)?)*')

        self.debug_enabled = \
            self.vim.vars['deoplete#sources#jedi#debug_enabled']

        self.description_length = \
            self.vim.vars['deoplete#sources#jedi#statement_length']

        self.use_short_types = \
            self.vim.vars['deoplete#sources#jedi#short_types']

        self.show_docstring = \
            self.vim.vars['deoplete#sources#jedi#show_docstring']

        self.worker_threads = \
            self.vim.vars['deoplete#sources#jedi#worker_threads']

        self.python_path = \
            self.vim.vars['deoplete#sources#jedi#python_path']

        self.workers_started = False
        self.boilerplate = []  # Completions that are included in all results

    def get_complete_position(self, context):
        pattern = r'\w*$'
        if context['input'].lstrip().startswith(('from ', 'import ')):
            m = re.search(r'[,\s]$', context['input'])
            if m:
                return m.end()
        m = re.search(pattern, context['input'])
        return m.start() if m else -1

    def mix_boilerplate(self, completions):
        seen = set()
        for item in self.boilerplate + completions:
            if item['word'] in seen:
                continue
            seen.add(item['word'])
            yield item

    @profiler.profile
    def gather_candidates(self, context):
        if not self.workers_started:
            if self.python_path and 'VIRTUAL_ENV' not in os.environ:
                cache.python = self.python_path
            worker.start(max(1, self.worker_threads), self.description_length,
                         self.use_short_types, self.show_docstring,
                         self.debug_enabled, self.python_path)
            cache.start_background(worker.comp_queue)
            self.workers_started = True

        refresh_boilerplate = False
        if not self.boilerplate:
            bp = cache.retrieve(('boilerplate~',))
            if bp:
                self.boilerplate = bp.completions[:]
                refresh_boilerplate = True
            else:
                # This should be the first time any completion happened, so
                # `wait` will be True.
                worker.work_queue.put((('boilerplate~',), [], '', 1, 0, ''))

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
        cached = cache.retrieve(cache_key)
        if cached and not cached.refresh:
            modules = cached.modules
            if all([filename in modules for filename in extra_modules]) \
                    and all([int(os.path.getmtime(filename)) == mtime
                             for filename, mtime in modules.items()]):
                # The cache is still valid
                refresh = False

        if cache_key and (cache_key[-1] in ('dot', 'vars', 'import', 'import~') or
                          (cached and cache_key[-1] == 'package' and
                           not len(cached.modules))):
            # Always refresh scoped variables and module imports.  Additionally
            # refresh cached items that did not have associated module files.
            refresh = True

        if (not cached or refresh) and cache_key and cache_key[-1] == 'package':
            # Make a synthetic completion for a module to guarantee the correct
            # completions.
            src = ['from {} import '.format(cache_key[0])]
            self.debug('source: %r', src)
            line = 1
            col = len(src[0])

        if cached is None:
            wait = True

        self.debug('Key: %r, Refresh: %r, Wait: %r', cache_key, refresh, wait)
        if cache_key and (not cached or refresh):
            n = time.time()
            worker.work_queue.put((cache_key, extra_modules, '\n'.join(src),
                                   line, col, str(buf.name)))
            while wait and time.time() - n < 2:
                cached = cache.retrieve(cache_key)
                if cached and cached.time >= n:
                    break
                time.sleep(0.01)

        if refresh_boilerplate:
            # This should only occur the first time completions happen.
            # Refresh the boilerplate to ensure it's always up to date (just in
            # case).
            self.debug('Refreshing boilerplate')
            worker.work_queue.put((('boilerplate~',), [], '', 1, 0, ''))

        if cached:
            if cached.completions is None:
                out = self.mix_boilerplate([])
            elif cache_key[-1] == 'vars':
                out = self.mix_boilerplate(cached.completions)
            else:
                out = cached.completions
            if filters:
                out = (x for x in out if x['$type'] in filters)
            return [x for x in sorted(out, key=sort_key)]
        return []
