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

    def gather_candidates(self, context):
        line = context['position'][1]
        col = context['complete_position']
        buf = self.vim.current.buffer
        source = buf[:]
        cline = self.vim.current.line

        extra_modules = []
        cache_key = None
        deoplete_input = context['input'].strip()
        cache_line = 0

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

            if context.get('complete_str'):
                # Note: Module completions will be an empty string.
                word = context.get('complete_str')
                cache_key = buf.name
                extra_modules.append(buf.name)
                cache_line = line - 1
            else:
                dot = deoplete_input.rfind('.')
                if dot != -1:
                    # Only cache module results.
                    cache_key = deoplete_input[:dot]
                    if cache_key.startswith('self'):
                        # TODO: Get class lines and cache these differently
                        # based on cursor position.
                        # Cache `self.`, but monitor buffer file's modification
                        # time.
                        extra_modules.append(buf.name)
                        cache_key = '{}.{}'.format(buf.name, cache_key)
                        cache_line = line - 1
                else:
                    extra_modules.append(buf.name)
                    cache_key = '{}.{}'.format(buf.name, deoplete_input)

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
                    return cached.get('completions', [])

        try:
            completions = \
                jedi.Script('\n'.join(source), line, col, buf.name).completions()
        except Exception:
            return []

        out = []
        modules = {f: int(os.path.getmtime(f)) for f in extra_modules}
        for c in completions:
            if c.module_path and c.module_path not in modules:
                modules[c.module_path] = int(os.path.getmtime(c.module_path))

            _type = c.type
            word = c.name

            # TODO(zchee): configurable and refactoring
            # Format c.docstring() for abbr
            if re.match(c.name, c.docstring()):
                abbr = re.sub('"(|)|  ",', '',
                              c.docstring().split("\n\n")[0]
                              .split("->")[0]
                              .replace('\n', ' ')
                              )
            else:
                abbr = word

            # Add '(' bracket
            if _type == 'function':
                word += '('
            # Add '.' for 'self' and 'class'
            elif not self.is_import(cline) and \
                    _type in ['module', 'class'] or \
                    not re.search(r'Error|Exception', word) and \
                    word == 'self':
                word += '.'

            out.append(dict(word=word,
                            abbr=abbr,
                            kind=self.format_description(c.description),
                            info=c.docstring(),
                            dup=1
                            ))

        if cache_key:
            lines = [0, 0]
            if cache_line:
                lines = self.indent_bounds(line, source)

            self.cache[cache_key] = {
                'lines': lines,
                'modules': modules,
                'completions': out,
            }

        return out

    def is_import(self, line):
        return re.match(r'^\s*from\s.+import \w*|'
                        r'^\s*from \w*|'
                        r'^\s*import \w*',
                        line)

    def format_description(self, raw_desc):
        description = re.sub('\n|  ', '', raw_desc)
        if re.search(' #', description) or \
                len(raw_desc) > self.description_length:
            description = 'statement'

        return description
