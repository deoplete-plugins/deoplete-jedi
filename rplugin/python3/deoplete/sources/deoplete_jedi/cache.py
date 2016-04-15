import os
import re
import time
import logging
import hashlib
import threading

_cache_lock = threading.RLock()
_cache = {}

log = logging.getLogger('deoplete.jedi.cache')

# This uses [\ \t] to avoid spanning lines
_import_re = re.compile(r'''
    ^[\ \t]*(
        from[\ \t]+[\w\.]+[\ \t]+import\s+\([\s\w,]+\)|
        from[\ \t]+[\w\.]+[\ \t]+import[\ \t\w,]+|
        import[\ \t]+\([\s\w,]+\)|
        import[\ \t]+[\ \t\w,]+
    )
''', re.VERBOSE | re.MULTILINE)


class CacheEntry(object):
    def __init__(self, dict):
        self.key = dict.get('cache_key')
        self._touched = time.time()
        self.time = dict.get('time')
        self.modules = dict.get('modules')
        self.completions = dict.get('completions')

    def touch(self):
        with _cache_lock:
            self._touched = time.time()


def retrieve(key):
    with _cache_lock:
        return _cache.get(key)


def store(key, value):
    with _cache_lock:
        if not isinstance(value, CacheEntry):
            value = CacheEntry(value)
        _cache[key] = value


def exists(key):
    return key in _cache


def reaper(max_age=300):
    """Clear the cache of old items

    Module level completions are exempt from reaping.  It is assumed that
    module level completions will have a key length of 1.
    """
    last = time.time()
    while True:
        n = time.time()
        if n - last < 30:
            time.sleep(0.05)
            continue
        last = n

        with _cache_lock:
            cl = len(_cache)
            for cached in list(_cache.values()):
                if len(cached.key) > 1 and n - cached._touched > max_age:
                    _cache.pop(cached.key)
            reaped = cl - len(_cache)
            if reaped > 0:
                log.debug('Removed %d of %d cache items', reaped, cl)


def start_reaper():
    log.debug('Starting reaper thread')
    t = threading.Thread(target=reaper)
    t.daemon = True
    t.start()


def split_module(text, default_value=None):
    """Utility to split the module text.

    If there is nothing to split, return `default_value`.
    """
    m = re.search('([\S\.]+)$', text)
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


def full_module(source, obj):
    """Construct the full module path

    This finds all imports and attempts to reconstruct the full module path.
    If matched on a standard `import` line, `obj` itself is a full module path.
    On `from` import lines, the parent module is prepended to `obj`.
    """

    module = ''
    obj_pat = r'\b{0}\b'.format(re.escape(obj))
    for match in _import_re.finditer('\n'.join(source)):
        module = ''
        imp_line = ' '.join(match.group(0).split())
        if imp_line.startswith('from '):
            _, module, imp_line = imp_line.split(' ', 2)
        if re.search(obj_pat, imp_line):
            if module:
                return '.'.join((module, obj))
            return obj
    return None


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
    filename_hash = hashlib.md5(filename.encode('utf8')).hexdigest()
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
                cache_key = (filename_hash, 'from', m.group(1))
            else:
                m = re.search(r'^from\s+(\S+)$', deoplete_input)

        if not cache_key and m:
            suffix = split_module(m.group(1), suffix)
            cache_key = (filename_hash, 'import', suffix)
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
                cache_key = (filename_hash, tuple(parents), obj)
            else:
                module_path = full_module(source, obj)
                if module_path and not module_path.startswith('.'):
                    cache_key = (module_path,)
                else:
                    # A quick scan revealed that the dot completion doesn't
                    # involve an imported module.  Treat it like a scoped
                    # variable and ensure the cache invalidates when the file
                    # is saved.
                    parents = get_parents(source, line)
                    parents.insert(0, cur_module)
                    cache_key = (filename_hash, tuple(parents), obj, 'dot')
                    if os.path.exists(filename):
                        extra_modules.append(filename)
        elif context.get('complete_str'):
            parents = get_parents(source, line)
            parents.insert(0, cur_module)
            cache_key = (filename_hash, tuple(parents), 'vars')
            if os.path.exists(filename):
                extra_modules.append(filename)

    return cache_key, extra_modules
