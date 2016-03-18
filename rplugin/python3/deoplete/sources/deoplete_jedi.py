import os
import re

from deoplete.sources.base import Base
from deoplete.util import load_external_module

current = __file__
load_external_module(current, 'jedi')
load_external_module(current, 'sources/deoplete_jedi')
import jedi


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

        self.description_length = \
            self.vim.vars['deoplete#sources#jedi#statement_length']

        self.cache_enabled = \
            self.vim.vars['deoplete#sources#jedi#enable_cache']

        self.complete_min_length = \
            self.vim.vars['deoplete#auto_complete_start_length']

        # jedi core library settings
        # http://jedi.jedidjah.ch/en/latest/docs/settings.html
        jedi_settings = jedi.settings
        # Completion output
        jedi_settings.case_insensitive_completion = False
        # Filesystem cache
        cache_home = os.getenv('XDG_CACHE_HOME')
        if cache_home is None:
            cache_home = os.path.expanduser('~/.cache')
        jedi_settings.cache_directory = os.path.join(cache_home, 'jedi')
        # Dynamic stuff
        jedi_settings.additional_dynamic_modules = [
            b.name for b in self.vim.buffers
            if b.name is not None and b.name.endswith('.py')]

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

    def split_module(self, text, default_value=None):
        """Utility to split the module text.

        If there is nothing to split, return `default_value`.
        """
        m = re.search('([\w_\.]+)$', text)
        if m and '.' in m.group(1):
            return m.group(1).rsplit('.', 1)[0]
        return default_value

    def gather_candidates(self, context):
        line = context['position'][1]
        col = context['complete_position']
        buf = self.vim.current.buffer
        src = buf[:]

        extra_modules = []
        cache_key = None
        deoplete_input = context['input'].strip()
        cache_line = 0

        # Inclusion filters for the results
        filters = []

        if re.match('^\s*(from|import)\s+', context['input']) \
                and not re.match('^\s*from\s+\S+\s+', context['input']):
            # If starting an import, only show module results
            filters.append('module')

        if self.cache_enabled:
            # Caching based on context input.  If the input is blank, it was
            # triggered with `.` to get module completions.
            #
            # The module files as reported by Jedi are stored with their
            # modification times to help detect if a cache needs to be
            # refreshed.
            #
            # For scoped variables in the buffer, construct a cache key using
            # the filename.  The buffer file's modification time is checked to
            # see if the completion needs to be refreshed.  The approximate
            # scope lines are cached to help invalidate the cache based on line
            # position.

            if deoplete_input.startswith(('import ', 'from ')):
                # Cache imports with buffer filename as the key prefix.
                # For `from` imports, the first part of the statement is
                # considered to be the same as `import` for caching.
                suffix = 'import'

                # The trailing whitespace is significant for caching imports.
                import_line = context['input'].lstrip()

                if import_line.startswith('import'):
                    m = re.search(r'^import\s+(\S+)$', import_line)
                else:
                    m = re.search(r'^from\s+(\S+)\s+import\s+', import_line)
                    if m:
                        # Treat the first part of the import as a cached
                        # module, but cache it per-buffer.
                        cache_key = '{}.from.{}'.format(buf.name, m.group(1))
                    else:
                        m = re.search(r'^from\s+(\S+)$', import_line)

                if not cache_key:
                    if m:
                        suffix = self.split_module(m.group(1), suffix)
                        cache_key = '{}.import.{}'.format(buf.name, suffix)
                        extra_modules.append(buf.name)

            if not cache_key:
                # Find a cacheable key first
                cache_key = self.split_module(deoplete_input)
                if cache_key:
                    if cache_key.startswith('self'):
                        # TODO: Get class lines and cache these differently
                        # based on cursor position.
                        # Cache `self.`, but monitor buffer file's modification
                        # time.
                        extra_modules.append(buf.name)
                        cache_key = '{}.{}'.format(buf.name, cache_key)
                        cache_line = line - 1
                        os.path
                elif context.get('complete_str'):
                    # Note: Module completions will be an empty string.
                    word = context.get('complete_str')
                    cache_key = buf.name
                    extra_modules.append(buf.name)
                    cache_line = line - 1

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
            completions = \
                jedi.Script('\n'.join(src), line, col, buf.name).completions()
        except Exception:
            return []

        out = []
        modules = {f: int(os.path.getmtime(f)) for f in extra_modules}
        for c in completions:
            if c.module_path and c.module_path not in modules:
                modules[c.module_path] = int(os.path.getmtime(c.module_path))

            _type = c.type
            docstring = c.docstring()

            word = c.name

            # TODO(zchee): configurable and refactoring
            # TODO(tweekmonster): Results for property types are incorrect.
            # Format c.docstring() for abbr
            if re.match(c.name, docstring):
                abbr = re.sub('"(|)|  ",', '',
                              docstring.split("\n\n")[0]
                              .split("->")[0]
                              .replace('\n', ' ')
                              )
            else:
                abbr = word

            out.append({
                '$type': _type,
                'word': word,
                'abbr': abbr,
                'kind': self.format_description(c.description),
                'info': docstring,
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
