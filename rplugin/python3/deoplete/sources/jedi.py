import os
import re
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
jedi_dir = os.path.join(os.path.dirname(current_dir), 'jedi')
sys.path.insert(0, jedi_dir)
import jedi

from .base import Base


class Source(Base):

    def __init__(self, vim):
        Base.__init__(self, vim)

        self.name = 'jedi'
        self.mark = '[jedi]'
        self.filetypes = ['python']
        self.input_pattern = (r'[^. \t0-9]\.\w*|^\s*@\w*|' +
                              r'^\s*from\s.+import \w*|' +
                              r'^\s*from \w*|^\s*import \w*')

    def get_complete_position(self, context):
        return self.completions(1, 0)

    def gather_candidates(self, context):
        return self.completions(0, 0)

    def get_script(self, source=None, column=None):
        # http://jedi.jedidjah.ch/en/latest/docs/settings.html#jedi.settings.add_dot_after_module
        # Adds a dot after a module, because a module that is not accessed this
        # way is definitely not the normal case.  However, in VIM this doesn’t
        # work, that’s why it isn’t used at the moment.
        jedi.settings.add_dot_after_module = True

        # http://jedi.jedidjah.ch/en/latest/docs/settings.html#jedi.settings.add_bracket_after_function
        # Adds an opening bracket after a function, because that's normal
        # behaviour.  Removed it again, because in VIM that is not very
        # practical.
        jedi.settings.add_bracket_after_function = True

        # http://jedi.jedidjah.ch/en/latest/docs/settings.html#jedi.settings.additional_dynamic_modules
        # Additional modules in which Jedi checks if statements are to be
        # found.  This is practical for IDEs, that want to administrate their
        # modules themselves.
        jedi.settings.additional_dynamic_modules = [
            b.name for b in self.vim.buffers
            if b.name is not None and b.name.endswith('.py')]

        cache_home = os.getenv('XDG_CACHE_HOME')
        if not cache_home:
            cache_home = '~/.cache'
        jedi.settings.cache_directory = os.path.join(cache_home, 'jedi')

        # Needed?
        if source is None:
            source = '\n'.join(self.vim.current.buffer)
        row = self.vim.current.window.cursor[0]
        # Needed?
        if column is None:
            column = self.vim.current.window.cursor[1]
        buf_path = self.vim.current.buffer.name
        encoding = self.vim.eval('&encoding')

        return jedi.Script(source, row, column, buf_path, encoding)

    def completions(self, findstart, base):
        row, column = self.vim.current.window.cursor
        currnt_line = self.vim.current.line

        if findstart == 1:
            count = 0
            for char in reversed(currnt_line[:column]):
                if not re.match('[\w\d]', char):
                    break
                count += 1
            return (column - count)

        # jedi-vim style? or simple?
        # source = '\n'.join(self.vim.current.buffer[:])
        source = ''
        for i, line in enumerate(self.vim.current.buffer):
            if i == row - 1:
                source += line[:column] + str(base) + line[column:]
            else:
                source += line
            source += '\n'

        out = []
        try:
            script = self.get_script(source=source, column=column)
        except:
            return out
        completions = script.completions()

        for c in completions:
            word = c.name
            abbr = c.name
            kind = re.sub('\n|  ', '', c.description)
            info = c.docstring()

            # TODO: configurable and refactoring
            # Add '(' bracket
            if c.type == 'function':
                word = c.name + '('
            # Add '.' for 'self' and 'class'
            elif word == 'self' or c.type == r'class|module' and not \
                    re.match(
                        r'^\s*from\s.+import \w*' +
                        '^\s*from \w*|^\s*import \w*',
                        currnt_line
                    ):
                word = c.name + '.'

            # Format c.docstring() for abbr
            if re.match(c.name, c.docstring()):
                abbr = re.sub('"(|)|  ",', '',
                              c.docstring().split("\n\n")[0]
                              .split("->")[0]
                              .replace('\n', ' ')
                              )

            out.append(dict(word=word,
                            abbr=abbr,
                            kind=kind,
                            info=info,
                            icase=1,
                            dup=1
                            ))

        return out
