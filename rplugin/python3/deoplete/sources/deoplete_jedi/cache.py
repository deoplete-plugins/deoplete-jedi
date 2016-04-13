import os
import re

import logging

log = logging.getLogger('deoplete.jedi.cache')


def split_module(text, default_value=None):
    """Utility to split the module text.

    If there is nothing to split, return `default_value`.
    """
    m = re.search('([\w_\.]+)$', text)
    if m and '.' in m.group(1):
        return m.group(1).rsplit('.', 1)[0]
    return default_value


def get_parents(source, line, class_only=False):
    """Find the parent blocks

    Collects parent blocks that contain the current line to help form a cache
    key based on variable scope.
    """
    parents = []
    start = line - 1
    indent = len(source[start]) - len(source[start].lstrip())
    if class_only:
        pattern = r'^\s*class\s+(\w+)'
    else:
        pattern = r'^\s*(?:def|class)\s+(\w+)'

    for i in range(start, 0, -1):
        s_line = source[i].lstrip()
        l_indent = len(source[i]) - len(s_line)
        if s_line and l_indent < indent:
            m = re.search(pattern, s_line)
            indent = l_indent
            if m:
                parents.insert(0, m.group(1))

    return parents


def cache_context(filename, context, source):
    """Caching based on context input.

    If the input is blank, it was triggered with `.` to get module completions.

    The module files as reported by Jedi are stored with their modification
    times to help detect if a cache needs to be refreshed.

    For scoped variables in the buffer, construct a cache key using the
    filename.  The buffer file's modification time is checked to see if the
    completion needs to be refreshed.  The approximate scope lines are cached
    to help invalidate the cache based on line position.

    Cache keys are made using tuples to make them easier to interpret later.
    """
    line = context['position'][1]
    deoplete_input = context['input'].lstrip()
    cache_key = None
    extra_modules = []

    if deoplete_input.startswith(('import ', 'from ')):
        # Cache imports with buffer filename as the key prefix.
        # For `from` imports, the first part of the statement is
        # considered to be the same as `import` for caching.
        suffix = 'import'

        # The trailing whitespace is significant for caching imports.
        deoplete_input = context['input'].lstrip()

        if deoplete_input.startswith('import'):
            m = re.search(r'^import\s+(\S+)$', deoplete_input)
            if not m:
                # There shouldn't be an object that is named 'import', but add
                # ~ at the end to prevent `import.` from showing completions.
                cache_key = ('import~',)
        else:
            m = re.search(r'^from\s+(\S+)\s+import\s+', deoplete_input)
            if m:
                # Treat the first part of the import as a cached
                # module, but cache it per-buffer.
                cache_key = (filename, 'from', m.group(1))
            else:
                m = re.search(r'^from\s+(\S+)$', deoplete_input)

        if not cache_key and m:
            suffix = split_module(m.group(1), suffix)
            cache_key = (filename, 'import', suffix)
            if os.path.exists(filename):
                extra_modules.append(filename)

    if not cache_key:
        obj = split_module(deoplete_input.strip())
        cur_module = os.path.basename(filename)
        cur_module = os.path.splitext(cur_module)[0]

        if obj:
            cache_key = (obj,)
            if obj.startswith('self'):
                if os.path.exists(filename):
                    extra_modules.append(filename)
                # `self` is a special case object that needs a scope included
                # in the cache key.
                parents = get_parents(source, line, class_only=True)
                parents.insert(0, cur_module)
                cache_key = (filename, tuple(parents), obj)
        elif context.get('complete_str'):
            parents = get_parents(source, line)
            parents.insert(0, cur_module)
            cache_key = (filename, tuple(parents), 'vars')
            if os.path.exists(filename):
                extra_modules.append(filename)

    return cache_key, extra_modules
