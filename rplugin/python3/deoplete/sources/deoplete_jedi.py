import os
import re
import sys

sys.path.insert(1, os.path.dirname(__file__))

from deoplete.sources.base import Base
from deoplete_jedi import server
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

        self.cache_enabled = \
            self.vim.vars['deoplete#sources#jedi#enable_cache']

        self.show_docstring = \
            self.vim.vars['deoplete#sources#jedi#show_docstring']

        self.complete_min_length = \
            self.vim.vars['deoplete#auto_complete_start_length']

        self._client = server.Client(self.description_length,
                                     self.use_short_types, self.show_docstring,
                                     self.debug_enabled)

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

    def gather_candidates(self, context):
        line = context['position'][1]
        col = context['complete_position']
        buf = self.vim.current.buffer
        src = buf[:]

        extra_modules = []
        cache_key = None
        cache_line = 0

        # Inclusion filters for the results
        filters = []

        if re.match('^\s*(from|import)\s+', context['input']) \
                and not re.match('^\s*from\s+\S+\s+', context['input']):
            # If starting an import, only show module results
            filters.append('module')

        if self.cache_enabled:
            cache_key, cache_line, extra_modules = cache_context(buf.name,
                                                                 context)

            if cache_key and cache_key in self.cache:
                # XXX: Hash cache keys to reduce length?
                cached = self.cache.get(cache_key)
                lines = cached.get('lines', [0, 0])
                modules = cached.get('modules')
                # TODO: If cache is invalid, return stale results and start
                # a thread to refresh.
                if cache_line >= lines[0] and cache_line <= lines[1] \
                        and all([filename in modules for filename in
                                 extra_modules]) \
                        and all([int(os.path.getmtime(filename)) == mtime
                                 for filename, mtime in modules.items()]):
                    out = cached.get('completions', [])
                    if filters:
                        return [x for x in out if x['$type'] in filters]
                    return out

        try:
            completions = self._client.completions('\n'.join(src), line, col,
                                                   str(buf.name))
        except Exception:
            return []

        out = []
        modules = {f: int(os.path.getmtime(f)) for f in extra_modules}
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
                'menu': self.mark + ' ',
                'dup': 1,
            })

        if cache_key:
            lines = [0, 0]
            if cache_line:
                lines = self.indent_bounds(line, src)

            self.cache[cache_key] = {
                'lines': lines,
                'modules': modules,
                'completions': out,
            }

        if filters:
            return [x for x in out if x['$type'] in filters]
        return out

    def format_description(self, raw_desc):
        description = re.sub('\n|  ', '', raw_desc)
        if len(description) > self.description_length:
            description = description[:self.description_length]

        return description
