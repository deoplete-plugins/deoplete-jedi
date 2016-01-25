# -*- coding: utf-8 -*-
"""
    sphinx.builders
    ~~~~~~~~~~~~~~~

    Builder superclass for all builders.

    :copyright: Copyright 2007-2015 by the Sphinx team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""

import os
import osutil
from os import path

try:
    import multiprocessing
    import threading
except ImportError:
    multiprocessing = threading = None

from docutils import nodes

from sphinx.util import i18n, path_stabilize
from sphinx.util.osutil import SEP, relative_uri
from sphinx.util.i18n import find_catalog
from sphinx.util.console import bold, darkgreen
from sphinx.util.parallel import ParallelTasks, SerialTasks, make_chunks, \
    parallel_available

# side effect: registers roles and directives
from sphinx import roles       # noqa
from sphinx import directives  # noqa


class Builder(object):
    """
    Builds target formats from the reST sources.
    """

    # builder's name, for the -b command line options
    name = ''
    # builder's output format, or '' if no document output is produced
    format = ''
    # doctree versioning method
    versioning_method = 'none'
    versioning_compare = False
    # allow parallel write_doc() calls
    allow_parallel = False

    def __init__(self, app):
        self.env = app.env
        self.env.set_versioning_method(self.versioning_method,
                                       self.versioning_compare)
        self.srcdir = app.srcdir
        self.confdir = app.confdir
        self.outdir = app.outdir
        self.doctreedir = app.doctreedir
        if not path.isdir(self.doctreedir):
            os.makedirs(self.doctreedir)

        self.app = app
        self.warn = app.warn
        self.info = app.info
        self.config = app.config
        self.tags = app.tags
        self.tags.add(self.format)
        self.tags.add(self.name)
        self.tags.add("format_%s" % self.format)
        self.tags.add("builder_%s" % self.name)
        # compatibility aliases
        self.status_iterator = app.status_iterator
        self.old_status_iterator = app.old_status_iterator

        # images that need to be copied over (source -> dest)
        self.images = {}
        # basename of images directory
        self.imagedir = ""
        # relative path to image directory from current docname (used at writing docs)
        self.imgpath = ""

        # these get set later
        self.parallel_ok = False
        self.finish_tasks = None

        # load default translator class
        self.translator_class = app._translators.get(self.name)

        self.init()

    # helper methods
    def init(self):
        """Load necessary templates and perform initialization.  The default
        implementation does nothing.
        """
        pass

    def create_template_bridge(self):
        """Return the template bridge configured."""
        if self.config.template_bridge:
            self.templates = self.app.import_object(
                self.config.template_bridge, 'template_bridge setting')()
        else:
            from sphinx.jinja2glue import BuiltinTemplateLoader
            self.templates = BuiltinTemplateLoader()

    def get_target_uri(self, docname, typ=None):
        """Return the target URI for a document name.

        *typ* can be used to qualify the link characteristic for individual
        builders.
        """
        raise NotImplementedError

    def get_relative_uri(self, from_, to, typ=None):
        """Return a relative URI between two source filenames.

        May raise environment.NoUri if there's no way to return a sensible URI.
        """
        return relative_uri(self.get_target_uri(from_),
                            self.get_target_uri(to, typ))

    def get_outdated_docs(self):
        """Return an iterable of output files that are outdated, or a string
        describing what an update build will build.

        If the builder does not output individual files corresponding to
        source files, return a string here.  If it does, return an iterable
        of those files that need to be written.
        """
        raise NotImplementedError

    supported_image_types = []

    def post_process_images(self, doctree):
        """Pick the best candidate for all image URIs."""
        for node in doctree.traverse(nodes.image):
            if '?' in node['candidates']:
                # don't rewrite nonlocal image URIs
                continue
            if '*' not in node['candidates']:
                for imgtype in self.supported_image_types:
                    candidate = node['candidates'].get(imgtype, None)
                    if candidate:
                        break
                else:
                    self.warn(
                        'no matching candidate for image URI %r' % node['uri'],
                        '%s:%s' % (node.source, getattr(node, 'line', '')))
                    continue
                node['uri'] = candidate
            else:
                candidate = node['uri']
            if candidate not in self.env.images:
                # non-existing URI; let it alone
                continue
            self.images[candidate] = self.env.images[candidate][1]

    # compile po methods

    def compile_catalogs(self, catalogs, message):
        if not self.config.gettext_auto_build:
            return

        def cat2relpath(cat):
            return path.relpath(cat.mo_path, self.env.srcdir).replace(path.sep, SEP)

        self.info(bold('building [mo]: ') + message)
        for catalog in self.app.status_iterator(
                catalogs, 'writing output... ', darkgreen, len(catalogs),
                cat2relpath):
            catalog.write_mo(self.config.language)

    def compile_all_catalogs(self):
        catalogs = i18n.find_catalog_source_files(
            [path.join(self.srcdir, x) for x in self.config.locale_dirs],
            self.config.language,
            charset=self.config.source_encoding,
            gettext_compact=self.config.gettext_compact,
            force_all=True)
        message = 'all of %d po files' % len(catalogs)
        self.compile_catalogs(catalogs, message)

    def compile_specific_catalogs(self, specified_files):
        def to_domain(fpath):
            docname, _ = path.splitext(path_stabilize(fpath))
            dom = find_catalog(docname, self.config.gettext_compact)
            return dom

        specified_domains = set(map(to_domain, specified_files))
        catalogs = i18n.find_catalog_source_files(
            [path.join(self.srcdir, x) for x in self.config.locale_dirs],
            self.config.language,
            domains=list(specified_domains),
            charset=self.config.source_encoding,
            gettext_compact=self.config.gettext_compact)
        message = 'targets for %d po files that are specified' % len(catalogs)
        self.compile_catalogs(catalogs, message)

    def compile_update_catalogs(self):
        catalogs = i18n.find_catalog_source_files(
            [path.join(self.srcdir, x) for x in self.config.locale_dirs],
            self.config.language,
            charset=self.config.source_encoding,
            gettext_compact=self.config.gettext_compact)
        message = 'targets for %d po files that are out of date' % len(catalogs)
        self.compile_catalogs(catalogs, message)

    # build methods

    def build_all(self):
        """Build all source files."""
        self.build(None, summary='all source files', method='all')

    def build_specific(self, filenames):
        """Only rebuild as much as needed for changes in the *filenames*."""
        # bring the filenames to the canonical format, that is,
        # relative to the source directory and without source_suffix.
        dirlen = len(self.srcdir) + 1
        to_write = []
        suffixes = tuple(self.config.source_suffix)
        for filename in filenames:
            filename = path.normpath(path.abspath(filename))
            if not filename.startswith(self.srcdir):
                self.warn('file %r given on command line is not under the '
                          'source directory, ignoring' % filename)
                continue
            if not (path.isfile(filename) or
                    any(path.isfile(filename + suffix) for suffix in suffixes)):
                self.warn('file %r given on command line does not exist, '
                          'ignoring' % filename)
                continue
            filename = filename[dirlen:]
            for suffix in suffixes:
                if filename.endswith(suffix):
                    filename = filename[:-len(suffix)]
                    break
            filename = filename.replace(path.sep, SEP)
            to_write.append(filename)
        self.build(to_write, method='specific',
                   summary='%d source files given on command '
                   'line' % len(to_write))

    def build_update(self):
        """Only rebuild what was changed or added since last build."""
        to_build = self.get_outdated_docs()
        if isinstance(to_build, str):
            self.build(['__all__'], to_build)
        else:
            to_build = list(to_build)
            self.build(to_build,
                       summary='targets for %d source files that are '
                       'out of date' % len(to_build))

    def build(self, docnames, summary=None, method='update'):
        """Main build method.

        First updates the environment, and then calls :meth:`write`.
        """
        if summary:
            self.info(bold('building [%s]' % self.name) + ': ' + summary)

        # while reading, collect all warnings from docutils
        warnings = []
        self.env.set_warnfunc(lambda *args: warnings.append(args))
        updated_docnames = set(self.env.update(self.config, self.srcdir,
                                               self.doctreedir, self.app))
        self.env.set_warnfunc(self.warn)
        for warning in warnings:
            self.warn(*warning)

        doccount = len(updated_docnames)
        self.info(bold('looking for now-outdated files... '), nonl=1)
        for docname in self.env.check_dependents(updated_docnames):
            updated_docnames.add(docname)
        outdated = len(updated_docnames) - doccount
        if outdated:
            self.info('%d found' % outdated)
        else:
            self.info('none found')

        if updated_docnames:
            # save the environment
            from sphinx.application import ENV_PICKLE_FILENAME
            self.info(bold('pickling environment... '), nonl=True)
            self.env.topickle(path.join(self.doctreedir, ENV_PICKLE_FILENAME))
            self.info('done')

            # global actions
            self.info(bold('checking consistency... '), nonl=True)
            self.env.check_consistency()
            self.info('done')
        else:
            if method == 'update' and not docnames:
                self.info(bold('no targets are out of date.'))
                return

        # filter "docnames" (list of outdated files) by the updated
        # found_docs of the environment; this will remove docs that
        # have since been removed
        if docnames and docnames != ['__all__']:
            docnames = set(docnames) & self.env.found_docs

        # determine if we can write in parallel
        self.parallel_ok = False
        if parallel_available and self.app.parallel > 1 and self.allow_parallel:
            self.parallel_ok = True
            for extname, md in self.app._extension_metadata.items():
                par_ok = md.get('parallel_write_safe', True)
                if not par_ok:
                    self.app.warn('the %s extension is not safe for parallel '
                                  'writing, doing serial write' % extname)
                    self.parallel_ok = False
                    break

        #  create a task executor to use for misc. "finish-up" tasks
        # if self.parallel_ok:
        #     self.finish_tasks = ParallelTasks(self.app.parallel)
        # else:
        # for now, just execute them serially
        self.finish_tasks = SerialTasks()

        # write all "normal" documents (or everything for some builders)
        self.write(docnames, list(updated_docnames), method)

        # finish (write static files etc.)
        self.finish()

        # wait for all tasks
        self.finish_tasks.join()

    def write(self, build_docnames, updated_docnames, method='update'):
        if build_docnames is None or build_docnames == ['__all__']:
            # build_all
            build_docnames = self.env.found_docs
        if method == 'update':
            # build updated ones as well
            docnames = set(build_docnames) | set(updated_docnames)
        else:
            docnames = set(build_docnames)
        self.app.debug('docnames to write: %s', ', '.join(sorted(docnames)))

        # add all toctree-containing files that may have changed
        for docname in list(docnames):
            for tocdocname in self.env.files_to_rebuild.get(docname, []):
                if tocdocname in self.env.found_docs:
                    docnames.add(tocdocname)
        docnames.add(self.config.master_doc)

        self.info(bold('preparing documents... '), nonl=True)
        self.prepare_writing(docnames)
        self.info('done')

        warnings = []
        self.env.set_warnfunc(lambda *args: warnings.append(args))
        if self.parallel_ok:
            # number of subprocesses is parallel-1 because the main process
            # is busy loading doctrees and doing write_doc_serialized()
            self._write_parallel(sorted(docnames), warnings,
                                 nproc=self.app.parallel - 1)
        else:
            self._write_serial(sorted(docnames), warnings)
        self.env.set_warnfunc(self.warn)

    def _write_serial(self, docnames, warnings):
        for docname in self.app.status_iterator(
                docnames, 'writing output... ', darkgreen, len(docnames)):
            doctree = self.env.get_and_resolve_doctree(docname, self)
            self.write_doc_serialized(docname, doctree)
            self.write_doc(docname, doctree)
        for warning in warnings:
            self.warn(*warning)

    def _write_parallel(self, docnames, warnings, nproc):
        def write_process(docs):
            local_warnings = []
            self.env.set_warnfunc(lambda *args: local_warnings.append(args))
            for docname, doctree in docs:
                self.write_doc(docname, doctree)
            return local_warnings

        def add_warnings(docs, wlist):
            warnings.extend(wlist)

        # warm up caches/compile templates using the first document
        firstname, docnames = docnames[0], docnames[1:]
        doctree = self.env.get_and_resolve_doctree(firstname, self)
        self.write_doc_serialized(firstname, doctree)
        self.write_doc(firstname, doctree)

        tasks = ParallelTasks(nproc)
        chunks = make_chunks(docnames, nproc)

        for chunk in self.app.status_iterator(
                chunks, 'writing output... ', darkgreen, len(chunks)):
            arg = []
            for i, docname in enumerate(chunk):
                doctree = self.env.get_and_resolve_doctree(docname, self)
                self.write_doc_serialized(docname, doctree)
                arg.append((docname, doctree))
            tasks.add_task(write_process, arg, add_warnings)

        # make sure all threads have finished
        self.info(bold('waiting for workers...'))
        tasks.join()

        for warning in warnings:
            self.warn(*warning)

    def prepare_writing(self, docnames):
        """A place where you can add logic before :meth:`write_doc` is run"""
        raise NotImplementedError

    def write_doc(self, docname, doctree):
        """Where you actually write something to the filesystem."""
        raise NotImplementedError

    def write_doc_serialized(self, docname, doctree):
        """Handle parts of write_doc that must be called in the main process
        if parallel build is active.
        """
        pass

    def finish(self):
        """Finish the building process.

        The default implementation does nothing.
        """
        pass

    def cleanup(self):
        """Cleanup any resources.

        The default implementation does nothing.
        """
        pass

    def get_builder_config(self, option, default):
        """Return a builder specific option.

        This method allows customization of common builder settings by
        inserting the name of the current builder in the option key.
        If the key does not exist, use default as builder name.
        """
        # At the moment, only XXX_use_index is looked up this way.
        # Every new builder variant must be registered in Config.config_values.
        try:
            optname = '%s_%s' % (self.name, option)
            return getattr(self.config, optname)
        except AttributeError:
            optname = '%s_%s' % (default, option)
            return getattr(self.config, optname)

BUILTIN_BUILDERS = {
    'html':       ('html', 'StandaloneHTMLBuilder'),
    'dirhtml':    ('html', 'DirectoryHTMLBuilder'),
    'singlehtml': ('html', 'SingleFileHTMLBuilder'),
    'pickle':     ('html', 'PickleHTMLBuilder'),
    'json':       ('html', 'JSONHTMLBuilder'),
    'web':        ('html', 'PickleHTMLBuilder'),
    'htmlhelp':   ('htmlhelp', 'HTMLHelpBuilder'),
    'devhelp':    ('devhelp', 'DevhelpBuilder'),
    'qthelp':     ('qthelp', 'QtHelpBuilder'),
    'applehelp':  ('applehelp', 'AppleHelpBuilder'),
    'epub':       ('epub', 'EpubBuilder'),
    'latex':      ('latex', 'LaTeXBuilder'),
    'text':       ('text', 'TextBuilder'),
    'man':        ('manpage', 'ManualPageBuilder'),
    'texinfo':    ('texinfo', 'TexinfoBuilder'),
    'changes':    ('changes', 'ChangesBuilder'),
    'linkcheck':  ('linkcheck', 'CheckExternalLinksBuilder'),
    'websupport': ('websupport', 'WebSupportBuilder'),
    'gettext':    ('gettext', 'MessageCatalogBuilder'),
    'xml':        ('xml', 'XMLBuilder'),
    'pseudoxml':  ('xml', 'PseudoXMLBuilder'),
}
# -*- coding: utf-8 -*-
"""
    sphinx.__main__
    ~~~~~~~~~~~~~~~

    The Sphinx documentation toolchain.

    :copyright: Copyright 2007-2015 by the Sphinx team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""
import sys
from sphinx import main

sys.exit(main(sys.argv))
# -*- coding: utf-8 -*-
"""
    sphinx.addnodes
    ~~~~~~~~~~~~~~~

    Additional docutils nodes.

    :copyright: Copyright 2007-2015 by the Sphinx team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""

from docutils import nodes


class toctree(nodes.General, nodes.Element):
    """Node for inserting a "TOC tree"."""


# domain-specific object descriptions (class, function etc.)

class desc(nodes.Admonition, nodes.Element):
    """Node for object descriptions.

    This node is similar to a "definition list" with one definition.  It
    contains one or more ``desc_signature`` and a ``desc_content``.
    """


class desc_signature(nodes.Part, nodes.Inline, nodes.TextElement):
    """Node for object signatures.

    The "term" part of the custom Sphinx definition list.
    """


# nodes to use within a desc_signature

class desc_addname(nodes.Part, nodes.Inline, nodes.TextElement):
    """Node for additional name parts (module name, class name)."""
# compatibility alias
desc_classname = desc_addname


class desc_type(nodes.Part, nodes.Inline, nodes.TextElement):
    """Node for return types or object type names."""


class desc_returns(desc_type):
    """Node for a "returns" annotation (a la -> in Python)."""
    def astext(self):
        return ' -> ' + nodes.TextElement.astext(self)


class desc_name(nodes.Part, nodes.Inline, nodes.TextElement):
    """Node for the main object name."""


class desc_parameterlist(nodes.Part, nodes.Inline, nodes.TextElement):
    """Node for a general parameter list."""
    child_text_separator = ', '


class desc_parameter(nodes.Part, nodes.Inline, nodes.TextElement):
    """Node for a single parameter."""


class desc_optional(nodes.Part, nodes.Inline, nodes.TextElement):
    """Node for marking optional parts of the parameter list."""
    child_text_separator = ', '

    def astext(self):
        return '[' + nodes.TextElement.astext(self) + ']'


class desc_annotation(nodes.Part, nodes.Inline, nodes.TextElement):
    """Node for signature annotations (not Python 3-style annotations)."""


class desc_content(nodes.General, nodes.Element):
    """Node for object description content.

    This is the "definition" part of the custom Sphinx definition list.
    """


# new admonition-like constructs

class versionmodified(nodes.Admonition, nodes.TextElement):
    """Node for version change entries.

    Currently used for "versionadded", "versionchanged" and "deprecated"
    directives.
    """


class seealso(nodes.Admonition, nodes.Element):
    """Custom "see also" admonition."""


class productionlist(nodes.Admonition, nodes.Element):
    """Node for grammar production lists.

    Contains ``production`` nodes.
    """


class production(nodes.Part, nodes.Inline, nodes.TextElement):
    """Node for a single grammar production rule."""


# other directive-level nodes

class index(nodes.Invisible, nodes.Inline, nodes.TextElement):
    """Node for index entries.

    This node is created by the ``index`` directive and has one attribute,
    ``entries``.  Its value is a list of 4-tuples of ``(entrytype, entryname,
    target, ignored)``.

    *entrytype* is one of "single", "pair", "double", "triple".
    """


class centered(nodes.Part, nodes.TextElement):
    """Deprecated."""


class acks(nodes.Element):
    """Special node for "acks" lists."""


class hlist(nodes.Element):
    """Node for "horizontal lists", i.e. lists that should be compressed to
    take up less vertical space.
    """


class hlistcol(nodes.Element):
    """Node for one column in a horizontal list."""


class compact_paragraph(nodes.paragraph):
    """Node for a compact paragraph (which never makes a <p> node)."""


class glossary(nodes.Element):
    """Node to insert a glossary."""


class only(nodes.Element):
    """Node for "only" directives (conditional inclusion based on tags)."""


# meta-information nodes

class start_of_file(nodes.Element):
    """Node to mark start of a new file, used in the LaTeX builder only."""


class highlightlang(nodes.Element):
    """Inserted to set the highlight language and line number options for
    subsequent code blocks.
    """


class tabular_col_spec(nodes.Element):
    """Node for specifying tabular columns, used for LaTeX output."""


class meta(nodes.Special, nodes.PreBibliographic, nodes.Element):
    """Node for meta directive -- same as docutils' standard meta node,
    but pickleable.
    """


# inline nodes

class pending_xref(nodes.Inline, nodes.Element):
    """Node for cross-references that cannot be resolved without complete
    information about all documents.

    These nodes are resolved before writing output, in
    BuildEnvironment.resolve_references.
    """


class number_reference(nodes.reference):
    """Node for number references, similar to pending_xref."""


class download_reference(nodes.reference):
    """Node for download references, similar to pending_xref."""


class literal_emphasis(nodes.emphasis):
    """Node that behaves like `emphasis`, but further text processors are not
    applied (e.g. smartypants for HTML output).
    """


class literal_strong(nodes.strong):
    """Node that behaves like `strong`, but further text processors are not
    applied (e.g. smartypants for HTML output).
    """


class abbreviation(nodes.Inline, nodes.TextElement):
    """Node for abbreviations with explanations."""


class termsep(nodes.Structural, nodes.Element):
    """Separates two terms within a <term> node."""


# make the new nodes known to docutils; needed because the HTML writer will
# choke at some point if these are not added
nodes._add_node_class_names(k for k in globals().keys()
                            if k != 'nodes' and k[0] != '_')
# -*- coding: utf-8 -*-
"""
    sphinx.apidoc
    ~~~~~~~~~~~~~

    Parses a directory tree looking for Python modules and packages and creates
    ReST files appropriately to create code documentation with Sphinx.  It also
    creates a modules index (named modules.<suffix>).

    This is derived from the "sphinx-autopackage" script, which is:
    Copyright 2008 Société des arts technologiques (SAT),
    http://www.sat.qc.ca/

    :copyright: Copyright 2007-2015 by the Sphinx team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""
# automodule options
if 'SPHINX_APIDOC_OPTIONS' in os.environ:
    OPTIONS = os.environ['SPHINX_APIDOC_OPTIONS'].split(',')
else:
    OPTIONS = [
        'members',
        'undoc-members',
        # 'inherited-members', # disabled because there's a bug in sphinx
        'show-inheritance',
    ]

INITPY = '__init__.py'
PY_SUFFIXES = set(['.py', '.pyx'])


def makename(package, module):
    """Join package and module with a dot."""
    # Both package and module can be None/empty.
    if package:
        name = package
        if module:
            name += '.' + module
    else:
        name = module
    return name


def write_file(name, text, opts):
    """Write the output file for module/package <name>."""
    fname = path.join(opts.destdir, '%s.%s' % (name, opts.suffix))
    if opts.dryrun:
        print('Would create file %s.' % fname)
        return
    if not opts.force and path.isfile(fname):
        print('File %s already exists, skipping.' % fname)
    else:
        print('Creating file %s.' % fname)
        f = open(fname, 'w')
        try:
            f.write(text)
        finally:
            f.close()


def format_heading(level, text):
    """Create a heading of <level> [1, 2 or 3 supported]."""
    underlining = ['=', '-', '~', ][level - 1] * len(text)
    return '%s\n%s\n\n' % (text, underlining)


def format_directive(module, package=None):
    """Create the automodule directive and add the options."""
    directive = '.. automodule:: %s\n' % makename(package, module)
    for option in OPTIONS:
        directive += '    :%s:\n' % option
    return directive


def create_module_file(package, module, opts):
    """Build the text of the file and write the file."""
    if not opts.noheadings:
        text = format_heading(1, '%s module' % module)
    else:
        text = ''
    # text += format_heading(2, ':mod:`%s` Module' % module)
    text += format_directive(module, package)
    write_file(makename(package, module), text, opts)


def create_package_file(root, master_package, subroot, py_files, opts, subs):
    """Build the text of the file and write the file."""
    text = format_heading(1, '%s package' % makename(master_package, subroot))

    if opts.modulefirst:
        text += format_directive(subroot, master_package)
        text += '\n'

    # build a list of directories that are szvpackages (contain an INITPY file)
    subs = [sub for sub in subs if path.isfile(path.join(root, sub, INITPY))]
    # if there are some package directories, add a TOC for theses subpackages
    if subs:
        text += format_heading(2, 'Subpackages')
        text += '.. toctree::\n\n'
        for sub in subs:
            text += '    %s.%s\n' % (makename(master_package, subroot), sub)
        text += '\n'

    submods = [path.splitext(sub)[0] for sub in py_files
               if not shall_skip(path.join(root, sub), opts) and
               sub != INITPY]
    if submods:
        text += format_heading(2, 'Submodules')
        if opts.separatemodules:
            text += '.. toctree::\n\n'
            for submod in submods:
                modfile = makename(master_package, makename(subroot, submod))
                text += '   %s\n' % modfile

                # generate separate file for this module
                if not opts.noheadings:
                    filetext = format_heading(1, '%s module' % modfile)
                else:
                    filetext = ''
                filetext += format_directive(makename(subroot, submod),
                                             master_package)
                write_file(modfile, filetext, opts)
        else:
            for submod in submods:
                modfile = makename(master_package, makename(subroot, submod))
                if not opts.noheadings:
                    text += format_heading(2, '%s module' % modfile)
                text += format_directive(makename(subroot, submod),
                                         master_package)
                text += '\n'
        text += '\n'

    if not opts.modulefirst:
        text += format_heading(2, 'Module contents')
        text += format_directive(subroot, master_package)

    write_file(makename(master_package, subroot), text, opts)


def create_modules_toc_file(modules, opts, name='modules'):
    """Create the module's index."""
    text = format_heading(1, '%s' % opts.header)
    text += '.. toctree::\n'
    text += '   :maxdepth: %s\n\n' % opts.maxdepth

    modules.sort()
    prev_module = ''
    for module in modules:
        # look if the module is a subpackage and, if yes, ignore it
        if module.startswith(prev_module + '.'):
            continue
        prev_module = module
        text += '   %s\n' % module

    write_file(name, text, opts)


def shall_skip(module, opts):
    """Check if we want to skip this module."""
    # skip it if there is nothing (or just \n or \r\n) in the file
    if path.getsize(module) <= 2:
        return True
    # skip if it has a "private" name and this is selected
    filename = path.basename(module)
    if filename != '__init__.py' and filename.startswith('_') and \
       not opts.includeprivate:
        return True
    return False


def recurse_tree(rootpath, excludes, opts):
    """
    Look for every file in the directory tree and create the corresponding
    ReST files.
    """
    # check if the base directory is a package and get its name
    if INITPY in os.listdir(rootpath):
        root_package = rootpath.split(path.sep)[-1]
    else:
        # otherwise, the base is a directory with packages
        root_package = None

    toplevels = []
    followlinks = getattr(opts, 'followlinks', False)
    includeprivate = getattr(opts, 'includeprivate', False)
    for root, subs, files in walk(rootpath, followlinks=followlinks):
        # document only Python module files (that aren't excluded)
        py_files = sorted(f for f in files
                          if path.splitext(f)[1] in PY_SUFFIXES and
                          not is_excluded(path.join(root, f), excludes))
        is_pkg = INITPY in py_files
        if is_pkg:
            py_files.remove(INITPY)
            py_files.insert(0, INITPY)
        elif root != rootpath:
            # only accept non-package at toplevel
            del subs[:]
            continue
        # remove hidden ('.') and private ('_') directories, as well as
        # excluded dirs
        if includeprivate:
            exclude_prefixes = ('.',)
        else:
            exclude_prefixes = ('.', '_')
        subs[:] = sorted(sub for sub in subs if not sub.startswith(exclude_prefixes) and
                         not is_excluded(path.join(root, sub), excludes))

        if is_pkg:
            # we are in a package with something to document
            if subs or len(py_files) > 1 or not \
               shall_skip(path.join(root, INITPY), opts):
                subpackage = root[len(rootpath):].lstrip(path.sep).\
                    replace(path.sep, '.')
                create_package_file(root, root_package, subpackage,
                                    py_files, opts, subs)
                toplevels.append(makename(root_package, subpackage))
        else:
            # if we are at the root level, we don't require it to be a package
            assert root == rootpath and root_package is None
            for py_file in py_files:
                if not shall_skip(path.join(rootpath, py_file), opts):
                    module = path.splitext(py_file)[0]
                    create_module_file(root_package, module, opts)
                    toplevels.append(module)

    return toplevels


def normalize_excludes(rootpath, excludes):
    """Normalize the excluded directory list."""
    return [path.abspath(exclude) for exclude in excludes]


def is_excluded(root, excludes):
    """Check if the directory is in the exclude list.

    Note: by having trailing slashes, we avoid common prefix issues, like
          e.g. an exlude "foo" also accidentally excluding "foobar".
    """
    for exclude in excludes:
        if root == exclude:
            return True
    return False


def main(argv=sys.argv):
    """Parse and check the command line arguments."""
    parser = optparse.OptionParser(
        usage="""\
usage: %prog [options] -o <output_path> <module_path> [exclude_path, ...]

Look recursively in <module_path> for Python modules and packages and create
one reST file with automodule directives per package in the <output_path>.

The <exclude_path>s can be files and/or directories that will be excluded
from generation.

Note: By default this script will not overwrite already created files.""")

    parser.add_option('-o', '--output-dir', action='store', dest='destdir',
                      help='Directory to place all output', default='')
    parser.add_option('-d', '--maxdepth', action='store', dest='maxdepth',
                      help='Maximum depth of submodules to show in the TOC '
                      '(default: 4)', type='int', default=4)
    parser.add_option('-f', '--force', action='store_true', dest='force',
                      help='Overwrite existing files')
    parser.add_option('-l', '--follow-links', action='store_true',
                      dest='followlinks', default=False,
                      help='Follow symbolic links. Powerful when combined '
                      'with collective.recipe.omelette.')
    parser.add_option('-n', '--dry-run', action='store_true', dest='dryrun',
                      help='Run the script without creating files')
    parser.add_option('-e', '--separate', action='store_true',
                      dest='separatemodules',
                      help='Put documentation for each module on its own page')
    parser.add_option('-P', '--private', action='store_true',
                      dest='includeprivate',
                      help='Include "_private" modules')
    parser.add_option('-T', '--no-toc', action='store_true', dest='notoc',
                      help='Don\'t create a table of contents file')
    parser.add_option('-E', '--no-headings', action='store_true',
                      dest='noheadings',
                      help='Don\'t create headings for the module/package '
                           'packages (e.g. when the docstrings already contain '
                           'them)')
    parser.add_option('-M', '--module-first', action='store_true',
                      dest='modulefirst',
                      help='Put module documentation before submodule '
                      'documentation')
    parser.add_option('-s', '--suffix', action='store', dest='suffix',
                      help='file suffix (default: rst)', default='rst')
    parser.add_option('-F', '--full', action='store_true', dest='full',
                      help='Generate a full project with sphinx-quickstart')
    parser.add_option('-H', '--doc-project', action='store', dest='header',
                      help='Project name (default: root module name)')
    parser.add_option('-A', '--doc-author', action='store', dest='author',
                      type='str',
                      help='Project author(s), used when --full is given')
    parser.add_option('-V', '--doc-version', action='store', dest='version',
                      help='Project version, used when --full is given')
    parser.add_option('-R', '--doc-release', action='store', dest='release',
                      help='Project release, used when --full is given, '
                      'defaults to --doc-version')
    parser.add_option('--version', action='store_true', dest='show_version',
                      help='Show version information and exit')

    (opts, args) = parser.parse_args(argv[1:])

    if opts.show_version:
        print('Sphinx (sphinx-apidoc) %s' % __display_version__)
        return 0

    if not args:
        parser.error('A package path is required.')

    rootpath, excludes = args[0], args[1:]
    if not opts.destdir:
        parser.error('An output directory is required.')
    if opts.header is None:
        opts.header = path.abspath(rootpath).split(path.sep)[-1]
    if opts.suffix.startswith('.'):
        opts.suffix = opts.suffix[1:]
    if not path.isdir(rootpath):
        print('%s is not a directory.' % rootpath, file=sys.stderr)
        sys.exit(1)
    if not path.isdir(opts.destdir):
        if not opts.dryrun:
            os.makedirs(opts.destdir)
    rootpath = path.abspath(rootpath)
    excludes = normalize_excludes(rootpath, excludes)
    modules = recurse_tree(rootpath, excludes, opts)
    if opts.full:
        from sphinx import quickstart as qs
        modules.sort()
        prev_module = ''
        text = ''
        for module in modules:
            if module.startswith(prev_module + '.'):
                continue
            prev_module = module
            text += '   %s\n' % module
        d = dict(
            path = opts.destdir,
            sep  = False,
            dot  = '_',
            project = opts.header,
            author = opts.author or 'Author',
            version = opts.version or '',
            release = opts.release or opts.version or '',
            suffix = '.' + opts.suffix,
            master = 'index',
            epub = True,
            ext_autodoc = True,
            ext_viewcode = True,
            ext_todo = True,
            makefile = True,
            batchfile = True,
            mastertocmaxdepth = opts.maxdepth,
            mastertoctree = text,
            language = 'en',
        )
        if not opts.dryrun:
            qs.generate(d, silent=True, overwrite=opts.force)
    elif not opts.notoc:
        create_modules_toc_file(modules, opts)
# -*- coding: utf-8 -*-
"""
    sphinx.builders.applehelp
    ~~~~~~~~~~~~~~~~~~~~~~~~~

    Build Apple help books.

    :copyright: Copyright 2007-2015 by the Sphinx team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""

# Use plistlib.dump in 3.4 and above
try:
    write_plist = plistlib.dump
except AttributeError:
    write_plist = plistlib.writePlist


# False access page (used because helpd expects strict XHTML)
access_page_template = '''\
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"\
 "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
  <head>
    <title>%(title)s</title>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
    <meta name="robots" content="noindex" />
    <meta http-equiv="refresh" content="0;url=%(toc)s" />
  </head>
  <body>
  </body>
</html>
'''


class AppleHelpIndexerFailed(SphinxError):
    category = 'Help indexer failed'


class AppleHelpCodeSigningFailed(SphinxError):
    category = 'Code signing failed'


class AppleHelpBuilder(StandaloneHTMLBuilder):
    """
    Builder that outputs an Apple help book.  Requires Mac OS X as it relies
    on the ``hiutil`` command line tool.
    """
    name = 'applehelp'

    # don't copy the reST source
    copysource = False
    supported_image_types = ['image/png', 'image/gif', 'image/jpeg',
                             'image/tiff', 'image/jp2', 'image/svg+xml']

    # don't add links
    add_permalinks = False

    # this is an embedded HTML format
    embedded = True

    # don't generate the search index or include the search page
    search = False

    def init(self):
        super(AppleHelpBuilder, self).init()
        # the output files for HTML help must be .html only
        self.out_suffix = '.html'

        if self.config.applehelp_bundle_id is None:
            raise SphinxError('You must set applehelp_bundle_id before '
                              'building Apple Help output')

        self.bundle_path = path.join(self.outdir,
                                     self.config.applehelp_bundle_name +
                                     '.help')
        self.outdir = path.join(self.bundle_path,
                                'Contents',
                                'Resources',
                                self.config.applehelp_locale + '.lproj')

    def handle_finish(self):
        super(AppleHelpBuilder, self).handle_finish()

        self.finish_tasks.add_task(self.copy_localized_files)
        self.finish_tasks.add_task(self.build_helpbook)

    def copy_localized_files(self):
        source_dir = path.join(self.confdir,
                               self.config.applehelp_locale + '.lproj')
        target_dir = self.outdir

        if path.isdir(source_dir):
            self.info(bold('copying localized files... '), nonl=True)

            ctx = self.globalcontext.copy()
            matchers = compile_matchers(self.config.exclude_patterns)
            copy_static_entry(source_dir, target_dir, self, ctx,
                              exclude_matchers=matchers)

            self.info('done')

    def build_helpbook(self):
        contents_dir = path.join(self.bundle_path, 'Contents')
        resources_dir = path.join(contents_dir, 'Resources')
        language_dir = path.join(resources_dir,
                                 self.config.applehelp_locale + '.lproj')

        for d in [contents_dir, resources_dir, language_dir]:
            ensuredir(d)

        # Construct the Info.plist file
        toc = self.config.master_doc + self.out_suffix

        info_plist = {
            'CFBundleDevelopmentRegion': self.config.applehelp_dev_region,
            'CFBundleIdentifier': self.config.applehelp_bundle_id,
            'CFBundleInfoDictionaryVersion': '6.0',
            'CFBundlePackageType': 'BNDL',
            'CFBundleShortVersionString': self.config.release,
            'CFBundleSignature': 'hbwr',
            'CFBundleVersion': self.config.applehelp_bundle_version,
            'HPDBookAccessPath': '_access.html',
            'HPDBookIndexPath': 'search.helpindex',
            'HPDBookTitle': self.config.applehelp_title,
            'HPDBookType': '3',
            'HPDBookUsesExternalViewer': False,
        }

        if self.config.applehelp_icon is not None:
            info_plist['HPDBookIconPath'] \
                = path.basename(self.config.applehelp_icon)

        if self.config.applehelp_kb_url is not None:
            info_plist['HPDBookKBProduct'] = self.config.applehelp_kb_product
            info_plist['HPDBookKBURL'] = self.config.applehelp_kb_url

        if self.config.applehelp_remote_url is not None:
            info_plist['HPDBookRemoteURL'] = self.config.applehelp_remote_url

        self.info(bold('writing Info.plist... '), nonl=True)
        with open(path.join(contents_dir, 'Info.plist'), 'wb') as f:
            write_plist(info_plist, f)
        self.info('done')

        # Copy the icon, if one is supplied
        if self.config.applehelp_icon:
            self.info(bold('copying icon... '), nonl=True)

            try:
                copyfile(path.join(self.srcdir, self.config.applehelp_icon),
                         path.join(resources_dir, info_plist['HPDBookIconPath']))

                self.info('done')
            except Exception as err:
                self.warn('cannot copy icon file %r: %s' %
                          (path.join(self.srcdir, self.config.applehelp_icon),
                           err))
                del info_plist['HPDBookIconPath']

        # Build the access page
        self.info(bold('building access page...'), nonl=True)
        f = codecs.open(path.join(language_dir, '_access.html'), 'w')
        try:
            f.write(access_page_template % {
                'toc': htmlescape(toc, quote=True),
                'title': htmlescape(self.config.applehelp_title)
            })
        finally:
            f.close()
        self.info('done')

        # Generate the help index
        self.info(bold('generating help index... '), nonl=True)

        args = [
            self.config.applehelp_indexer_path,
            '-Cf',
            path.join(language_dir, 'search.helpindex'),
            language_dir
        ]

        if self.config.applehelp_index_anchors is not None:
            args.append('-a')

        if self.config.applehelp_min_term_length is not None:
            args += ['-m', '%s' % self.config.applehelp_min_term_length]

        if self.config.applehelp_stopwords is not None:
            args += ['-s', self.config.applehelp_stopwords]

        if self.config.applehelp_locale is not None:
            args += ['-l', self.config.applehelp_locale]

        if self.config.applehelp_disable_external_tools:
            self.info('skipping')

            self.warn('you will need to index this help book with:\n  %s'
                      % (' '.join([pipes.quote(arg) for arg in args])))
        else:
            p = subprocess.Popen(args,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)

            output = p.communicate()[0]

            if p.returncode != 0:
                raise AppleHelpIndexerFailed(output)
            else:
                self.info('done')

        # If we've been asked to, sign the bundle
        if self.config.applehelp_codesign_identity:
            self.info(bold('signing help book... '), nonl=True)

            args = [
                self.config.applehelp_codesign_path,
                '-s', self.config.applehelp_codesign_identity,
                '-f'
            ]

            args += self.config.applehelp_codesign_flags

            args.append(self.bundle_path)

            if self.config.applehelp_disable_external_tools:
                self.info('skipping')

                self.warn('you will need to sign this help book with:\n  %s'
                          % (' '.join([pipes.quote(arg) for arg in args])))
            else:
                p = subprocess.Popen(args,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)

                output = p.communicate()[0]

                if p.returncode != 0:
                    raise AppleHelpCodeSigningFailed(output)
                else:
                    self.info('done')
# -*- coding: utf-8 -*-
"""
    sphinx.application
    ~~~~~~~~~~~~~~~~~~

    Sphinx application object.

    Gracefully adapted from the TextPress system by Armin.

    :copyright: Copyright 2007-2015 by the Sphinx team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""

if hasattr(sys, 'intern'):
    intern = sys.intern

# List of all known core events. Maps name to arguments description.
events = {
    'builder-inited': '',
    'env-get-outdated': 'env, added, changed, removed',
    'env-purge-doc': 'env, docname',
    'env-before-read-docs': 'env, docnames',
    'source-read': 'docname, source text',
    'doctree-read': 'the doctree before being pickled',
    'env-merge-info': 'env, read docnames, other env instance',
    'missing-reference': 'env, node, contnode',
    'doctree-resolved': 'doctree, docname',
    'env-updated': 'env',
    'html-collect-pages': 'builder',
    'html-page-context': 'pagename, context, doctree or None',
    'build-finished': 'exception',
}

CONFIG_FILENAME = 'conf.py'
ENV_PICKLE_FILENAME = 'environment.pickle'


class Sphinx(object):

    def __init__(self, srcdir, confdir, outdir, doctreedir, buildername,
                 confoverrides=None, status=sys.stdout, warning=sys.stderr,
                 freshenv=False, warningiserror=False, tags=None, verbosity=0,
                 parallel=0):
        self.verbosity = verbosity
        self.next_listener_id = 0
        self._extensions = {}
        self._extension_metadata = {}
        self._listeners = {}
        self.domains = BUILTIN_DOMAINS.copy()
        self.buildername = buildername
        self.builderclasses = BUILTIN_BUILDERS.copy()
        self.builder = None
        self.env = None

        self.srcdir = srcdir
        self.confdir = confdir
        self.outdir = outdir
        self.doctreedir = doctreedir

        self.parallel = parallel

        if status is None:
            self._status = cStringIO()
            self.quiet = True
        else:
            self._status = status
            self.quiet = False

        if warning is None:
            self._warning = cStringIO()
        else:
            self._warning = warning
        self._warncount = 0
        self.warningiserror = warningiserror

        self._events = events.copy()
        self._translators = {}

        # keep last few messages for traceback
        self.messagelog = deque(maxlen=10)

        # say hello to the world
        self.info(bold('Running Sphinx v%s' % sphinx.__display_version__))

        # status code for command-line application
        self.statuscode = 0

        if not path.isdir(outdir):
            self.info('making output directory...')
            os.makedirs(outdir)

        # read config
        self.tags = Tags(tags)
        self.config = Config(confdir, CONFIG_FILENAME,
                             confoverrides or {}, self.tags)
        self.config.check_unicode(self.warn)
        # defer checking types until i18n has been initialized

        # set confdir to srcdir if -C given (!= no confdir); a few pieces
        # of code expect a confdir to be set
        if self.confdir is None:
            self.confdir = self.srcdir

        # extension loading support for alabaster theme
        # self.config.html_theme is not set from conf.py at here
        # for now, sphinx always load a 'alabaster' extension.
        if 'alabaster' not in self.config.extensions:
            self.config.extensions.append('alabaster')

        # load all user-given extension modules
        for extension in self.config.extensions:
            self.setup_extension(extension)
        # the config file itself can be an extension
        if self.config.setup:
            # py31 doesn't have 'callable' function for below check
            if hasattr(self.config.setup, '__call__'):
                self.config.setup(self)
            else:
                raise ConfigError(
                    "'setup' that is specified in the conf.py has not been " +
                    "callable. Please provide a callable `setup` function " +
                    "in order to behave as a sphinx extension conf.py itself."
                )

        # now that we know all config values, collect them from conf.py
        self.config.init_values(self.warn)

        # check the Sphinx version if requested
        if self.config.needs_sphinx and \
           self.config.needs_sphinx > sphinx.__display_version__[:3]:
            raise VersionRequirementError(
                'This project needs at least Sphinx v%s and therefore cannot '
                'be built with this version.' % self.config.needs_sphinx)

        # check extension versions if requested
        if self.config.needs_extensions:
            for extname, needs_ver in self.config.needs_extensions.items():
                if extname not in self._extensions:
                    self.warn('needs_extensions config value specifies a '
                              'version requirement for extension %s, but it is '
                              'not loaded' % extname)
                    continue
                has_ver = self._extension_metadata[extname]['version']
                if has_ver == 'unknown version' or needs_ver > has_ver:
                    raise VersionRequirementError(
                        'This project needs the extension %s at least in '
                        'version %s and therefore cannot be built with the '
                        'loaded version (%s).' % (extname, needs_ver, has_ver))

        # set up translation infrastructure
        self._init_i18n()
        # check all configuration values for permissible types
        self.config.check_types(self.warn)
        # set up the build environment
        self._init_env(freshenv)
        # set up the builder
        self._init_builder(self.buildername)

    def _init_i18n(self):
        """Load translated strings from the configured localedirs if enabled in
        the configuration.
        """
        if self.config.language is not None:
            self.info(bold('loading translations [%s]... ' %
                           self.config.language), nonl=True)
            locale_dirs = [None, path.join(package_dir, 'locale')] + \
                [path.join(self.srcdir, x) for x in self.config.locale_dirs]
        else:
            locale_dirs = []
        self.translator, has_translation = locale.init(locale_dirs,
                                                       self.config.language,
                                                       charset=self.config.source_encoding)
        if self.config.language is not None:
            if has_translation or self.config.language == 'en':
                # "en" never needs to be translated
                self.info('done')
            else:
                self.info('not available for built-in messages')

    def _init_env(self, freshenv):
        if freshenv:
            self.env = BuildEnvironment(self.srcdir, self.doctreedir,
                                        self.config)
            self.env.find_files(self.config)
            for domain in self.domains.keys():
                self.env.domains[domain] = self.domains[domain](self.env)
        else:
            try:
                self.info(bold('loading pickled environment... '), nonl=True)
                self.env = BuildEnvironment.frompickle(
                    self.config, path.join(self.doctreedir, ENV_PICKLE_FILENAME))
                self.env.domains = {}
                for domain in self.domains.keys():
                    # this can raise if the data version doesn't fit
                    self.env.domains[domain] = self.domains[domain](self.env)
                self.info('done')
            except Exception as err:
                if isinstance(err, IOError) and err.errno == ENOENT:
                    self.info('not yet created')
                else:
                    self.info('failed: %s' % err)
                return self._init_env(freshenv=True)

        self.env.set_warnfunc(self.warn)

    def _init_builder(self, buildername):
        if buildername is None:
            print('No builder selected, using default: html', file=self._status)
            buildername = 'html'
        if buildername not in self.builderclasses:
            raise SphinxError('Builder name %s not registered' % buildername)

        builderclass = self.builderclasses[buildername]
        if isinstance(builderclass, tuple):
            # builtin builder
            mod, cls = builderclass
            builderclass = getattr(
                __import__('sphinx.builders.' + mod, None, None, [cls]), cls)
        self.builder = builderclass(self)
        self.emit('builder-inited')

    # ---- main "build" method -------------------------------------------------

    def build(self, force_all=False, filenames=None):
        try:
            if force_all:
                self.builder.compile_all_catalogs()
                self.builder.build_all()
            elif filenames:
                self.builder.compile_specific_catalogs(filenames)
                self.builder.build_specific(filenames)
            else:
                self.builder.compile_update_catalogs()
                self.builder.build_update()

            status = (self.statuscode == 0 and
                      'succeeded' or 'finished with problems')
            if self._warncount:
                self.info(bold('build %s, %s warning%s.' %
                               (status, self._warncount,
                                self._warncount != 1 and 's' or '')))
            else:
                self.info(bold('build %s.' % status))
        except Exception as err:
            # delete the saved env to force a fresh build next time
            envfile = path.join(self.doctreedir, ENV_PICKLE_FILENAME)
            if path.isfile(envfile):
                os.unlink(envfile)
            self.emit('build-finished', err)
            raise
        else:
            self.emit('build-finished', None)
        self.builder.cleanup()

    # ---- logging handling ----------------------------------------------------

    def _log(self, message, wfile, nonl=False):
        try:
            wfile.write(message)
        except UnicodeEncodeError:
            encoding = getattr(wfile, 'encoding', 'ascii') or 'ascii'
            wfile.write(message.encode(encoding, 'replace'))
        if not nonl:
            wfile.write('\n')
        if hasattr(wfile, 'flush'):
            wfile.flush()
        self.messagelog.append(message)

    def warn(self, message, location=None, prefix='WARNING: '):
        """Emit a warning.

        If *location* is given, it should either be a tuple of (docname, lineno)
        or a string describing the location of the warning as well as possible.

        *prefix* usually should not be changed.

        .. note::

           For warnings emitted during parsing, you should use
           :meth:`.BuildEnvironment.warn` since that will collect all
           warnings during parsing for later output.
        """
        if isinstance(location, tuple):
            docname, lineno = location
            if docname:
                location = '%s:%s' % (self.env.doc2path(docname), lineno or '')
            else:
                location = None
        warntext = location and '%s: %s%s\n' % (location, prefix, message) or \
            '%s%s\n' % (prefix, message)
        if self.warningiserror:
            raise SphinxWarning(warntext)
        self._warncount += 1
        self._log(warntext, self._warning, True)

    def info(self, message='', nonl=False):
        """Emit an informational message.

        If *nonl* is true, don't emit a newline at the end (which implies that
        more info output will follow soon.)
        """
        self._log(message, self._status, nonl)

    def verbose(self, message, *args, **kwargs):
        """Emit a verbose informational message.

        The message will only be emitted for verbosity levels >= 1 (i.e. at
        least one ``-v`` option was given).

        The message can contain %-style interpolation placeholders, which is
        formatted with either the ``*args`` or ``**kwargs`` when output.
        """
        if self.verbosity < 1:
            return
        if args or kwargs:
            message = message % (args or kwargs)
        self._log(message, self._status)

    def debug(self, message, *args, **kwargs):
        """Emit a debug-level informational message.

        The message will only be emitted for verbosity levels >= 2 (i.e. at
        least two ``-v`` options were given).

        The message can contain %-style interpolation placeholders, which is
        formatted with either the ``*args`` or ``**kwargs`` when output.
        """
        if self.verbosity < 2:
            return
        if args or kwargs:
            message = message % (args or kwargs)
        self._log(darkgray(message), self._status)

    def debug2(self, message, *args, **kwargs):
        """Emit a lowlevel debug-level informational message.

        The message will only be emitted for verbosity level 3 (i.e. three
        ``-v`` options were given).

        The message can contain %-style interpolation placeholders, which is
        formatted with either the ``*args`` or ``**kwargs`` when output.
        """
        if self.verbosity < 3:
            return
        if args or kwargs:
            message = message % (args or kwargs)
        self._log(lightgray(message), self._status)

    def _display_chunk(chunk):
        if isinstance(chunk, (list, tuple)):
            if len(chunk) == 1:
                return text_type(chunk[0])
            return '%s .. %s' % (chunk[0], chunk[-1])
        return text_type(chunk)

    def old_status_iterator(self, iterable, summary, colorfunc=darkgreen,
                            stringify_func=_display_chunk):
        l = 0
        for item in iterable:
            if l == 0:
                self.info(bold(summary), nonl=1)
                l = 1
            self.info(colorfunc(stringify_func(item)) + ' ', nonl=1)
            yield item
        if l == 1:
            self.info()

    # new version with progress info
    def status_iterator(self, iterable, summary, colorfunc=darkgreen, length=0,
                        stringify_func=_display_chunk):
        if length == 0:
            for item in self.old_status_iterator(iterable, summary, colorfunc,
                                                 stringify_func):
                yield item
            return
        l = 0
        summary = bold(summary)
        for item in iterable:
            l += 1
            s = '%s[%3d%%] %s' % (summary, 100*l/length,
                                  colorfunc(stringify_func(item)))
            if self.verbosity:
                s += '\n'
            else:
                s = term_width_line(s)
            self.info(s, nonl=1)
            yield item
        if l > 0:
            self.info()

    # ---- general extensibility interface -------------------------------------

    def setup_extension(self, extension):
        """Import and setup a Sphinx extension module. No-op if called twice."""
        self.debug('[app] setting up extension: %r', extension)
        if extension in self._extensions:
            return
        try:
            mod = __import__(extension, None, None, ['setup'])
        except ImportError as err:
            self.verbose('Original exception:\n' + traceback.format_exc())
            raise ExtensionError('Could not import extension %s' % extension,
                                 err)
        if not hasattr(mod, 'setup'):
            self.warn('extension %r has no setup() function; is it really '
                      'a Sphinx extension module?' % extension)
            ext_meta = None
        else:
            try:
                ext_meta = mod.setup(self)
            except VersionRequirementError as err:
                # add the extension name to the version required
                raise VersionRequirementError(
                    'The %s extension used by this project needs at least '
                    'Sphinx v%s; it therefore cannot be built with this '
                    'version.' % (extension, err))
        if ext_meta is None:
            ext_meta = {}
            # special-case for compatibility
            if extension == 'rst2pdf.pdfbuilder':
                ext_meta = {'parallel_read_safe': True}
        try:
            if not ext_meta.get('version'):
                ext_meta['version'] = 'unknown version'
        except Exception:
            self.warn('extension %r returned an unsupported object from '
                      'its setup() function; it should return None or a '
                      'metadata dictionary' % extension)
            ext_meta = {'version': 'unknown version'}
        self._extensions[extension] = mod
        self._extension_metadata[extension] = ext_meta

    def require_sphinx(self, version):
        # check the Sphinx version if requested
        if version > sphinx.__display_version__[:3]:
            raise VersionRequirementError(version)

    def import_object(self, objname, source=None):
        """Import an object from a 'module.name' string."""
        return import_object(objname, source=None)

    # event interface

    def _validate_event(self, event):
        event = intern(event)
        if event not in self._events:
            raise ExtensionError('Unknown event name: %s' % event)

    def connect(self, event, callback):
        self._validate_event(event)
        listener_id = self.next_listener_id
        if event not in self._listeners:
            self._listeners[event] = {listener_id: callback}
        else:
            self._listeners[event][listener_id] = callback
        self.next_listener_id += 1
        self.debug('[app] connecting event %r: %r [id=%s]',
                   event, callback, listener_id)
        return listener_id

    def disconnect(self, listener_id):
        self.debug('[app] disconnecting event: [id=%s]', listener_id)
        for event in itervalues(self._listeners):
            event.pop(listener_id, None)

    def emit(self, event, *args):
        try:
            self.debug2('[app] emitting event: %r%s', event, repr(args)[:100])
        except Exception:
            # not every object likes to be repr()'d (think
            # random stuff coming via autodoc)
            pass
        results = []
        if event in self._listeners:
            for _, callback in iteritems(self._listeners[event]):
                results.append(callback(self, *args))
        return results

    def emit_firstresult(self, event, *args):
        for result in self.emit(event, *args):
            if result is not None:
                return result
        return None

    # registering addon parts

    def add_builder(self, builder):
        self.debug('[app] adding builder: %r', builder)
        if not hasattr(builder, 'name'):
            raise ExtensionError('Builder class %s has no "name" attribute'
                                 % builder)
        if builder.name in self.builderclasses:
            if isinstance(self.builderclasses[builder.name], tuple):
                raise ExtensionError('Builder %r is a builtin builder' %
                                     builder.name)
            else:
                raise ExtensionError(
                    'Builder %r already exists (in module %s)' % (
                        builder.name, self.builderclasses[builder.name].__module__))
        self.builderclasses[builder.name] = builder

    def add_config_value(self, name, default, rebuild):
        self.debug('[app] adding config value: %r', (name, default, rebuild))
        if name in self.config.values:
            raise ExtensionError('Config value %r already present' % name)
        if rebuild in (False, True):
            rebuild = rebuild and 'env' or ''
        self.config.values[name] = (default, rebuild)

    def add_event(self, name):
        self.debug('[app] adding event: %r', name)
        if name in self._events:
            raise ExtensionError('Event %r already present' % name)
        self._events[name] = ''

    def set_translator(self, name, translator_class):
        self.info(bold('A Translator for the %s builder is changed.' % name))
        self._translators[name] = translator_class

    def add_node(self, node, **kwds):
        self.debug('[app] adding node: %r', (node, kwds))
        nodes._add_node_class_names([node.__name__])
        for key, val in iteritems(kwds):
            try:
                visit, depart = val
            except ValueError:
                raise ExtensionError('Value for key %r must be a '
                                     '(visit, depart) function tuple' % key)
            translator = self._translators.get(key)
            if translator is not None:
                pass
            elif key == 'html':
                from sphinx.writers.html import HTMLTranslator as translator
            elif key == 'latex':
                from sphinx.writers.latex import LaTeXTranslator as translator
            elif key == 'text':
                from sphinx.writers.text import TextTranslator as translator
            elif key == 'man':
                from sphinx.writers.manpage import ManualPageTranslator \
                    as translator
            elif key == 'texinfo':
                from sphinx.writers.texinfo import TexinfoTranslator \
                    as translator
            else:
                # ignore invalid keys for compatibility
                continue
            setattr(translator, 'visit_'+node.__name__, visit)
            if depart:
                setattr(translator, 'depart_'+node.__name__, depart)

    def _directive_helper(self, obj, content=None, arguments=None, **options):
        if isinstance(obj, (types.FunctionType, types.MethodType)):
            obj.content = content
            obj.arguments = arguments or (0, 0, False)
            obj.options = options
            return convert_directive_function(obj)
        else:
            if content or arguments or options:
                raise ExtensionError('when adding directive classes, no '
                                     'additional arguments may be given')
            return obj

    def add_directive(self, name, obj, content=None, arguments=None, **options):
        self.debug('[app] adding directive: %r',
                   (name, obj, content, arguments, options))
        directives.register_directive(
            name, self._directive_helper(obj, content, arguments, **options))

    def add_role(self, name, role):
        self.debug('[app] adding role: %r', (name, role))
        roles.register_local_role(name, role)

    def add_generic_role(self, name, nodeclass):
        # don't use roles.register_generic_role because it uses
        # register_canonical_role
        self.debug('[app] adding generic role: %r', (name, nodeclass))
        role = roles.GenericRole(name, nodeclass)
        roles.register_local_role(name, role)

    def add_domain(self, domain):
        self.debug('[app] adding domain: %r', domain)
        if domain.name in self.domains:
            raise ExtensionError('domain %s already registered' % domain.name)
        self.domains[domain.name] = domain

    def override_domain(self, domain):
        self.debug('[app] overriding domain: %r', domain)
        if domain.name not in self.domains:
            raise ExtensionError('domain %s not yet registered' % domain.name)
        if not issubclass(domain, self.domains[domain.name]):
            raise ExtensionError('new domain not a subclass of registered %s '
                                 'domain' % domain.name)
        self.domains[domain.name] = domain

    def add_directive_to_domain(self, domain, name, obj,
                                content=None, arguments=None, **options):
        self.debug('[app] adding directive to domain: %r',
                   (domain, name, obj, content, arguments, options))
        if domain not in self.domains:
            raise ExtensionError('domain %s not yet registered' % domain)
        self.domains[domain].directives[name] = \
            self._directive_helper(obj, content, arguments, **options)

    def add_role_to_domain(self, domain, name, role):
        self.debug('[app] adding role to domain: %r', (domain, name, role))
        if domain not in self.domains:
            raise ExtensionError('domain %s not yet registered' % domain)
        self.domains[domain].roles[name] = role

    def add_index_to_domain(self, domain, index):
        self.debug('[app] adding index to domain: %r', (domain, index))
        if domain not in self.domains:
            raise ExtensionError('domain %s not yet registered' % domain)
        self.domains[domain].indices.append(index)

    def add_object_type(self, directivename, rolename, indextemplate='',
                        parse_node=None, ref_nodeclass=None, objname='',
                        doc_field_types=[]):
        self.debug('[app] adding object type: %r',
                   (directivename, rolename, indextemplate, parse_node,
                    ref_nodeclass, objname, doc_field_types))
        StandardDomain.object_types[directivename] = \
            ObjType(objname or directivename, rolename)
        # create a subclass of GenericObject as the new directive
        new_directive = type(directivename, (GenericObject, object),
                             {'indextemplate': indextemplate,
                              'parse_node': staticmethod(parse_node),
                              'doc_field_types': doc_field_types})
        StandardDomain.directives[directivename] = new_directive
        # XXX support more options?
        StandardDomain.roles[rolename] = XRefRole(innernodeclass=ref_nodeclass)

    # backwards compatible alias
    add_description_unit = add_object_type

    def add_crossref_type(self, directivename, rolename, indextemplate='',
                          ref_nodeclass=None, objname=''):
        self.debug('[app] adding crossref type: %r',
                   (directivename, rolename, indextemplate, ref_nodeclass,
                    objname))
        StandardDomain.object_types[directivename] = \
            ObjType(objname or directivename, rolename)
        # create a subclass of Target as the new directive
        new_directive = type(directivename, (Target, object),
                             {'indextemplate': indextemplate})
        StandardDomain.directives[directivename] = new_directive
        # XXX support more options?
        StandardDomain.roles[rolename] = XRefRole(innernodeclass=ref_nodeclass)

    def add_transform(self, transform):
        self.debug('[app] adding transform: %r', transform)
        SphinxStandaloneReader.transforms.append(transform)

    def add_javascript(self, filename):
        self.debug('[app] adding javascript: %r', filename)
        from sphinx.builders.html import StandaloneHTMLBuilder
        if '://' in filename:
            StandaloneHTMLBuilder.script_files.append(filename)
        else:
            StandaloneHTMLBuilder.script_files.append(
                posixpath.join('_static', filename))

    def add_stylesheet(self, filename):
        self.debug('[app] adding stylesheet: %r', filename)
        from sphinx.builders.html import StandaloneHTMLBuilder
        if '://' in filename:
            StandaloneHTMLBuilder.css_files.append(filename)
        else:
            StandaloneHTMLBuilder.css_files.append(
                posixpath.join('_static', filename))

    def add_latex_package(self, packagename, options=None):
        self.debug('[app] adding latex package: %r', packagename)
        from sphinx.builders.latex import LaTeXBuilder
        LaTeXBuilder.usepackages.append((packagename, options))

    def add_lexer(self, alias, lexer):
        self.debug('[app] adding lexer: %r', (alias, lexer))
        from sphinx.highlighting import lexers
        if lexers is None:
            return
        lexers[alias] = lexer

    def add_autodocumenter(self, cls):
        self.debug('[app] adding autodocumenter: %r', cls)
        from sphinx.ext import autodoc
        autodoc.add_documenter(cls)
        self.add_directive('auto' + cls.objtype, autodoc.AutoDirective)

    def add_autodoc_attrgetter(self, type, getter):
        self.debug('[app] adding autodoc attrgetter: %r', (type, getter))
        from sphinx.ext import autodoc
        autodoc.AutoDirective._special_attrgetters[type] = getter

    def add_search_language(self, cls):
        self.debug('[app] adding search language: %r', cls)
        from sphinx.search import languages, SearchLanguage
        assert issubclass(cls, SearchLanguage)
        languages[cls.lang] = cls


class TemplateBridge(object):
    """
    This class defines the interface for a "template bridge", that is, a class
    that renders templates given a template name and a context.
    """

    def init(self, builder, theme=None, dirs=None):
        """Called by the builder to initialize the template system.

        *builder* is the builder object; you'll probably want to look at the
        value of ``builder.config.templates_path``.

        *theme* is a :class:`sphinx.theming.Theme` object or None; in the latter
        case, *dirs* can be list of fixed directories to look for templates.
        """
        raise NotImplementedError('must be implemented in subclasses')

    def newest_template_mtime(self):
        """Called by the builder to determine if output files are outdated
        because of template changes.  Return the mtime of the newest template
        file that was changed.  The default implementation returns ``0``.
        """
        return 0

    def render(self, template, context):
        """Called by the builder to render a template given as a filename with
        a specified context (a Python dictionary).
        """
        raise NotImplementedError('must be implemented in subclasses')

    def render_string(self, template, context):
        """Called by the builder to render a template given as a string with a
        specified context (a Python dictionary).
        """
        raise NotImplementedError('must be implemented in subclasses')
# -*- coding: utf-8 -*-
"""
    sphinx.ext.autodoc
    ~~~~~~~~~~~~~~~~~~

    Automatically insert docstrings for functions, classes or whole modules into
    the doctree, thus avoiding duplication between docstrings and documentation
    for those who like elaborate docstrings.

    :copyright: Copyright 2007-2015 by the Sphinx team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""


#: extended signature RE: with explicit module name separated by ::
py_ext_sig_re = re.compile(
    r'''^ ([\w.]+::)?            # explicit module name
          ([\w.]+\.)?            # module and/or class name(s)
          (\w+)  \s*             # thing name
          (?: \((.*)\)           # optional: arguments
           (?:\s* -> \s* (.*))?  #           return annotation
          )? $                   # and nothing more
          ''', re.VERBOSE)


class DefDict(dict):
    """A dict that returns a default on nonexisting keys."""
    def __init__(self, default):
        dict.__init__(self)
        self.default = default

    def __getitem__(self, key):
        try:
            return dict.__getitem__(self, key)
        except KeyError:
            return self.default

    def __bool__(self):
        # docutils check "if option_spec"
        return True
    __nonzero__ = __bool__  # for python2 compatibility


def identity(x):
    return x


class Options(dict):
    """A dict/attribute hybrid that returns None on nonexisting keys."""
    def __getattr__(self, name):
        try:
            return self[name.replace('_', '-')]
        except KeyError:
            return None


class _MockModule(object):
    """Used by autodoc_mock_imports."""
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return _MockModule()

    @classmethod
    def __getattr__(cls, name):
        if name in ('__file__', '__path__'):
            return '/dev/null'
        elif name[0] == name[0].upper():
            # Not very good, we assume Uppercase names are classes...
            mocktype = type(name, (), {})
            mocktype.__module__ = __name__
            return mocktype
        else:
            return _MockModule()


def mock_import(modname):
    if '.' in modname:
        pkg, _n, mods = modname.rpartition('.')
        mock_import(pkg)
    mod = _MockModule()
    sys.modules[modname] = mod
    return mod


ALL = object()
INSTANCEATTR = object()


def members_option(arg):
    """Used to convert the :members: option to auto directives."""
    if arg is None:
        return ALL
    return [x.strip() for x in arg.split(',')]


def members_set_option(arg):
    """Used to convert the :members: option to auto directives."""
    if arg is None:
        return ALL
    return set(x.strip() for x in arg.split(','))

SUPPRESS = object()


def annotation_option(arg):
    if arg is None:
        # suppress showing the representation of the object
        return SUPPRESS
    else:
        return arg


def bool_option(arg):
    """Used to convert flag options to auto directives.  (Instead of
    directives.flag(), which returns None).
    """
    return True


class AutodocReporter(object):
    """
    A reporter replacement that assigns the correct source name
    and line number to a system message, as recorded in a ViewList.
    """
    def __init__(self, viewlist, reporter):
        self.viewlist = viewlist
        self.reporter = reporter

    def __getattr__(self, name):
        return getattr(self.reporter, name)

    def system_message(self, level, message, *children, **kwargs):
        if 'line' in kwargs and 'source' not in kwargs:
            try:
                source, line = self.viewlist.items[kwargs['line']]
            except IndexError:
                pass
            else:
                kwargs['source'] = source
                kwargs['line'] = line
        return self.reporter.system_message(level, message,
                                            *children, **kwargs)

    def debug(self, *args, **kwargs):
        if self.reporter.debug_flag:
            return self.system_message(0, *args, **kwargs)

    def info(self, *args, **kwargs):
        return self.system_message(1, *args, **kwargs)

    def warning(self, *args, **kwargs):
        return self.system_message(2, *args, **kwargs)

    def error(self, *args, **kwargs):
        return self.system_message(3, *args, **kwargs)

    def severe(self, *args, **kwargs):
        return self.system_message(4, *args, **kwargs)


# Some useful event listener factories for autodoc-process-docstring.

def cut_lines(pre, post=0, what=None):
    """Return a listener that removes the first *pre* and last *post*
    lines of every docstring.  If *what* is a sequence of strings,
    only docstrings of a type in *what* will be processed.

    Use like this (e.g. in the ``setup()`` function of :file:`conf.py`)::

       from sphinx.ext.autodoc import cut_lines
       app.connect('autodoc-process-docstring', cut_lines(4, what=['module']))

    This can (and should) be used in place of :confval:`automodule_skip_lines`.
    """
    def process(app, what_, name, obj, options, lines):
        if what and what_ not in what:
            return
        del lines[:pre]
        if post:
            # remove one trailing blank line.
            if lines and not lines[-1]:
                lines.pop(-1)
            del lines[-post:]
        # make sure there is a blank line at the end
        if lines and lines[-1]:
            lines.append('')
    return process


def between(marker, what=None, keepempty=False, exclude=False):
    """Return a listener that either keeps, or if *exclude* is True excludes,
    lines between lines that match the *marker* regular expression.  If no line
    matches, the resulting docstring would be empty, so no change will be made
    unless *keepempty* is true.

    If *what* is a sequence of strings, only docstrings of a type in *what* will
    be processed.
    """
    marker_re = re.compile(marker)

    def process(app, what_, name, obj, options, lines):
        if what and what_ not in what:
            return
        deleted = 0
        delete = not exclude
        orig_lines = lines[:]
        for i, line in enumerate(orig_lines):
            if delete:
                lines.pop(i - deleted)
                deleted += 1
            if marker_re.match(line):
                delete = not delete
                if delete:
                    lines.pop(i - deleted)
                    deleted += 1
        if not lines and not keepempty:
            lines[:] = orig_lines
        # make sure there is a blank line at the end
        if lines and lines[-1]:
            lines.append('')
    return process


def formatargspec(*argspec):
    return inspect.formatargspec(*argspec,
                                 formatvalue=lambda x: '=' + object_description(x))


class Documenter(object):
    """
    A Documenter knows how to autodocument a single object type.  When
    registered with the AutoDirective, it will be used to document objects
    of that type when needed by autodoc.

    Its *objtype* attribute selects what auto directive it is assigned to
    (the directive name is 'auto' + objtype), and what directive it generates
    by default, though that can be overridden by an attribute called
    *directivetype*.

    A Documenter has an *option_spec* that works like a docutils directive's;
    in fact, it will be used to parse an auto directive's options that matches
    the documenter.
    """
    #: name by which the directive is called (auto...) and the default
    #: generated directive name
    objtype = 'object'
    #: indentation by which to indent the directive content
    content_indent = u'   '
    #: priority if multiple documenters return True from can_document_member
    priority = 0
    #: order if autodoc_member_order is set to 'groupwise'
    member_order = 0
    #: true if the generated content may contain titles
    titles_allowed = False

    option_spec = {'noindex': bool_option}

    @staticmethod
    def get_attr(obj, name, *defargs):
        """getattr() override for types such as Zope interfaces."""
        for typ, func in iteritems(AutoDirective._special_attrgetters):
            if isinstance(obj, typ):
                return func(obj, name, *defargs)
        return safe_getattr(obj, name, *defargs)

    @classmethod
    def can_document_member(cls, member, membername, isattr, parent):
        """Called to see if a member can be documented by this documenter."""
        raise NotImplementedError('must be implemented in subclasses')

    def __init__(self, directive, name, indent=u''):
        self.directive = directive
        self.env = directive.env
        self.options = directive.genopt
        self.name = name
        self.indent = indent
        # the module and object path within the module, and the fully
        # qualified name (all set after resolve_name succeeds)
        self.modname = None
        self.module = None
        self.objpath = None
        self.fullname = None
        # extra signature items (arguments and return annotation,
        # also set after resolve_name succeeds)
        self.args = None
        self.retann = None
        # the object to document (set after import_object succeeds)
        self.object = None
        self.object_name = None
        # the parent/owner of the object to document
        self.parent = None
        # the module analyzer to get at attribute docs, or None
        self.analyzer = None

    def add_line(self, line, source, *lineno):
        """Append one line of generated reST to the output."""
        self.directive.result.append(self.indent + line, source, *lineno)

    def resolve_name(self, modname, parents, path, base):
        """Resolve the module and name of the object to document given by the
        arguments and the current module/class.

        Must return a pair of the module name and a chain of attributes; for
        example, it would return ``('zipfile', ['ZipFile', 'open'])`` for the
        ``zipfile.ZipFile.open`` method.
        """
        raise NotImplementedError('must be implemented in subclasses')

    def parse_name(self):
        """Determine what module to import and what attribute to document.

        Returns True and sets *self.modname*, *self.objpath*, *self.fullname*,
        *self.args* and *self.retann* if parsing and resolving was successful.
        """
        # first, parse the definition -- auto directives for classes and
        # functions can contain a signature which is then used instead of
        # an autogenerated one
        try:
            explicit_modname, path, base, args, retann = \
                py_ext_sig_re.match(self.name).groups()
        except AttributeError:
            self.directive.warn('invalid signature for auto%s (%r)' %
                                (self.objtype, self.name))
            return False

        # support explicit module and class name separation via ::
        if explicit_modname is not None:
            modname = explicit_modname[:-2]
            parents = path and path.rstrip('.').split('.') or []
        else:
            modname = None
            parents = []

        self.modname, self.objpath = \
            self.resolve_name(modname, parents, path, base)

        if not self.modname:
            return False

        self.args = args
        self.retann = retann
        self.fullname = (self.modname or '') + \
                        (self.objpath and '.' + '.'.join(self.objpath) or '')
        return True

    def import_object(self):
        """Import the object given by *self.modname* and *self.objpath* and set
        it as *self.object*.

        Returns True if successful, False if an error occurred.
        """
        dbg = self.env.app.debug
        if self.objpath:
            dbg('[autodoc] from %s import %s',
                self.modname, '.'.join(self.objpath))
        try:
            dbg('[autodoc] import %s', self.modname)
            for modname in self.env.config.autodoc_mock_imports:
                dbg('[autodoc] adding a mock module %s!', self.modname)
                mock_import(modname)
            __import__(self.modname)
            parent = None
            obj = self.module = sys.modules[self.modname]
            dbg('[autodoc] => %r', obj)
            for part in self.objpath:
                parent = obj
                dbg('[autodoc] getattr(_, %r)', part)
                obj = self.get_attr(obj, part)
                dbg('[autodoc] => %r', obj)
                self.object_name = part
            self.parent = parent
            self.object = obj
            return True
        # this used to only catch SyntaxError, ImportError and AttributeError,
        # but importing modules with side effects can raise all kinds of errors
        except (Exception, SystemExit) as e:
            if self.objpath:
                errmsg = 'autodoc: failed to import %s %r from module %r' % \
                         (self.objtype, '.'.join(self.objpath), self.modname)
            else:
                errmsg = 'autodoc: failed to import %s %r' % \
                         (self.objtype, self.fullname)
            if isinstance(e, SystemExit):
                errmsg += ('; the module executes module level statement ' +
                           'and it might call sys.exit().')
            else:
                errmsg += '; the following exception was raised:\n%s' % \
                          traceback.format_exc()
            dbg(errmsg)
            self.directive.warn(errmsg)
            self.env.note_reread()
            return False

    def get_real_modname(self):
        """Get the real module name of an object to document.

        It can differ from the name of the module through which the object was
        imported.
        """
        return self.get_attr(self.object, '__module__', None) or self.modname

    def check_module(self):
        """Check if *self.object* is really defined in the module given by
        *self.modname*.
        """
        if self.options.imported_members:
            return True

        modname = self.get_attr(self.object, '__module__', None)
        if modname and modname != self.modname:
            return False
        return True

    def format_args(self):
        """Format the argument signature of *self.object*.

        Should return None if the object does not have a signature.
        """
        return None

    def format_name(self):
        """Format the name of *self.object*.

        This normally should be something that can be parsed by the generated
        directive, but doesn't need to be (Sphinx will display it unparsed
        then).
        """
        # normally the name doesn't contain the module (except for module
        # directives of course)
        return '.'.join(self.objpath) or self.modname

    def format_signature(self):
        """Format the signature (arguments and return annotation) of the object.

        Let the user process it via the ``autodoc-process-signature`` event.
        """
        if self.args is not None:
            # signature given explicitly
            args = "(%s)" % self.args
        else:
            # try to introspect the signature
            try:
                args = self.format_args()
            except Exception as err:
                self.directive.warn('error while formatting arguments for '
                                    '%s: %s' % (self.fullname, err))
                args = None

        retann = self.retann

        result = self.env.app.emit_firstresult(
            'autodoc-process-signature', self.objtype, self.fullname,
            self.object, self.options, args, retann)
        if result:
            args, retann = result

        if args is not None:
            return args + (retann and (' -> %s' % retann) or '')
        else:
            return ''

    def add_directive_header(self, sig):
        """Add the directive header and options to the generated content."""
        domain = getattr(self, 'domain', 'py')
        directive = getattr(self, 'directivetype', self.objtype)
        name = self.format_name()
        sourcename = self.get_sourcename()
        self.add_line(u'.. %s:%s:: %s%s' % (domain, directive, name, sig),
                      sourcename)
        if self.options.noindex:
            self.add_line(u'   :noindex:', sourcename)
        if self.objpath:
            # Be explicit about the module, this is necessary since .. class::
            # etc. don't support a prepended module name
            self.add_line(u'   :module: %s' % self.modname, sourcename)

    def get_doc(self, encoding=None, ignore=1):
        """Decode and return lines of the docstring(s) for the object."""
        docstring = self.get_attr(self.object, '__doc__', None)
        # make sure we have Unicode docstrings, then sanitize and split
        # into lines
        if isinstance(docstring, text_type):
            return [prepare_docstring(docstring, ignore)]
        elif isinstance(docstring, str):  # this will not trigger on Py3
            return [prepare_docstring(force_decode(docstring, encoding),
                                      ignore)]
        # ... else it is something strange, let's ignore it
        return []

    def process_doc(self, docstrings):
        """Let the user process the docstrings before adding them."""
        for docstringlines in docstrings:
            if self.env.app:
                # let extensions preprocess docstrings
                self.env.app.emit('autodoc-process-docstring',
                                  self.objtype, self.fullname, self.object,
                                  self.options, docstringlines)
            for line in docstringlines:
                yield line

    def get_sourcename(self):
        if self.analyzer:
            # prevent encoding errors when the file name is non-ASCII
            if not isinstance(self.analyzer.srcname, text_type):
                filename = text_type(self.analyzer.srcname,
                                     sys.getfilesystemencoding(), 'replace')
            else:
                filename = self.analyzer.srcname
            return u'%s:docstring of %s' % (filename, self.fullname)
        return u'docstring of %s' % self.fullname

    def add_content(self, more_content, no_docstring=False):
        """Add content from docstrings, attribute documentation and user."""
        # set sourcename and add content from attribute documentation
        sourcename = self.get_sourcename()
        if self.analyzer:
            attr_docs = self.analyzer.find_attr_docs()
            if self.objpath:
                key = ('.'.join(self.objpath[:-1]), self.objpath[-1])
                if key in attr_docs:
                    no_docstring = True
                    docstrings = [attr_docs[key]]
                    for i, line in enumerate(self.process_doc(docstrings)):
                        self.add_line(line, sourcename, i)

        # add content from docstrings
        if not no_docstring:
            encoding = self.analyzer and self.analyzer.encoding
            docstrings = self.get_doc(encoding)
            if not docstrings:
                # append at least a dummy docstring, so that the event
                # autodoc-process-docstring is fired and can add some
                # content if desired
                docstrings.append([])
            for i, line in enumerate(self.process_doc(docstrings)):
                self.add_line(line, sourcename, i)

        # add additional content (e.g. from document), if present
        if more_content:
            for line, src in zip(more_content.data, more_content.items):
                self.add_line(line, src[0], src[1])

    def get_object_members(self, want_all):
        """Return `(members_check_module, members)` where `members` is a
        list of `(membername, member)` pairs of the members of *self.object*.

        If *want_all* is True, return all members.  Else, only return those
        members given by *self.options.members* (which may also be none).
        """
        analyzed_member_names = set()
        if self.analyzer:
            attr_docs = self.analyzer.find_attr_docs()
            namespace = '.'.join(self.objpath)
            for item in iteritems(attr_docs):
                if item[0][0] == namespace:
                    analyzed_member_names.add(item[0][1])
        if not want_all:
            if not self.options.members:
                return False, []
            # specific members given
            members = []
            for mname in self.options.members:
                try:
                    members.append((mname, self.get_attr(self.object, mname)))
                except AttributeError:
                    if mname not in analyzed_member_names:
                        self.directive.warn('missing attribute %s in object %s'
                                            % (mname, self.fullname))
        elif self.options.inherited_members:
            # safe_getmembers() uses dir() which pulls in members from all
            # base classes
            members = safe_getmembers(self.object, attr_getter=self.get_attr)
        else:
            # __dict__ contains only the members directly defined in
            # the class (but get them via getattr anyway, to e.g. get
            # unbound method objects instead of function objects);
            # using keys() because apparently there are objects for which
            # __dict__ changes while getting attributes
            try:
                obj_dict = self.get_attr(self.object, '__dict__')
            except AttributeError:
                members = []
            else:
                members = [(mname, self.get_attr(self.object, mname, None))
                           for mname in obj_dict.keys()]
        membernames = set(m[0] for m in members)
        # add instance attributes from the analyzer
        for aname in analyzed_member_names:
            if aname not in membernames and \
               (want_all or aname in self.options.members):
                members.append((aname, INSTANCEATTR))
        return False, sorted(members)

    def filter_members(self, members, want_all):
        """Filter the given member list.

        Members are skipped if

        - they are private (except if given explicitly or the private-members
          option is set)
        - they are special methods (except if given explicitly or the
          special-members option is set)
        - they are undocumented (except if the undoc-members option is set)

        The user can override the skipping decision by connecting to the
        ``autodoc-skip-member`` event.
        """
        ret = []

        # search for members in source code too
        namespace = '.'.join(self.objpath)  # will be empty for modules

        if self.analyzer:
            attr_docs = self.analyzer.find_attr_docs()
        else:
            attr_docs = {}

        # process members and determine which to skip
        for (membername, member) in members:
            # if isattr is True, the member is documented as an attribute
            isattr = False

            doc = self.get_attr(member, '__doc__', None)
            # if the member __doc__ is the same as self's __doc__, it's just
            # inherited and therefore not the member's doc
            cls = self.get_attr(member, '__class__', None)
            if cls:
                cls_doc = self.get_attr(cls, '__doc__', None)
                if cls_doc == doc:
                    doc = None
            has_doc = bool(doc)

            keep = False
            if want_all and membername.startswith('__') and \
                    membername.endswith('__') and len(membername) > 4:
                # special __methods__
                if self.options.special_members is ALL and \
                        membername != '__doc__':
                    keep = has_doc or self.options.undoc_members
                elif self.options.special_members and \
                    self.options.special_members is not ALL and \
                        membername in self.options.special_members:
                    keep = has_doc or self.options.undoc_members
            elif want_all and membername.startswith('_'):
                # ignore members whose name starts with _ by default
                keep = self.options.private_members and \
                    (has_doc or self.options.undoc_members)
            elif (namespace, membername) in attr_docs:
                # keep documented attributes
                keep = True
                isattr = True
            else:
                # ignore undocumented members if :undoc-members: is not given
                keep = has_doc or self.options.undoc_members

            # give the user a chance to decide whether this member
            # should be skipped
            if self.env.app:
                # let extensions preprocess docstrings
                skip_user = self.env.app.emit_firstresult(
                    'autodoc-skip-member', self.objtype, membername, member,
                    not keep, self.options)
                if skip_user is not None:
                    keep = not skip_user

            if keep:
                ret.append((membername, member, isattr))

        return ret

    def document_members(self, all_members=False):
        """Generate reST for member documentation.

        If *all_members* is True, do all members, else those given by
        *self.options.members*.
        """
        # set current namespace for finding members
        self.env.temp_data['autodoc:module'] = self.modname
        if self.objpath:
            self.env.temp_data['autodoc:class'] = self.objpath[0]

        want_all = all_members or self.options.inherited_members or \
            self.options.members is ALL
        # find out which members are documentable
        members_check_module, members = self.get_object_members(want_all)

        # remove members given by exclude-members
        if self.options.exclude_members:
            members = [(membername, member) for (membername, member) in members
                       if membername not in self.options.exclude_members]

        # document non-skipped members
        memberdocumenters = []
        for (mname, member, isattr) in self.filter_members(members, want_all):
            classes = [cls for cls in itervalues(AutoDirective._registry)
                       if cls.can_document_member(member, mname, isattr, self)]
            if not classes:
                # don't know how to document this member
                continue
            # prefer the documenter with the highest priority
            classes.sort(key=lambda cls: cls.priority)
            # give explicitly separated module name, so that members
            # of inner classes can be documented
            full_mname = self.modname + '::' + \
                '.'.join(self.objpath + [mname])
            documenter = classes[-1](self.directive, full_mname, self.indent)
            memberdocumenters.append((documenter, isattr))
        member_order = self.options.member_order or \
            self.env.config.autodoc_member_order
        if member_order == 'groupwise':
            # sort by group; relies on stable sort to keep items in the
            # same group sorted alphabetically
            memberdocumenters.sort(key=lambda e: e[0].member_order)
        elif member_order == 'bysource' and self.analyzer:
            # sort by source order, by virtue of the module analyzer
            tagorder = self.analyzer.tagorder

            def keyfunc(entry):
                fullname = entry[0].name.split('::')[1]
                return tagorder.get(fullname, len(tagorder))
            memberdocumenters.sort(key=keyfunc)

        for documenter, isattr in memberdocumenters:
            documenter.generate(
                all_members=True, real_modname=self.real_modname,
                check_module=members_check_module and not isattr)

        # reset current objects
        self.env.temp_data['autodoc:module'] = None
        self.env.temp_data['autodoc:class'] = None

    def generate(self, more_content=None, real_modname=None,
                 check_module=False, all_members=False):
        """Generate reST for the object given by *self.name*, and possibly for
        its members.

        If *more_content* is given, include that content. If *real_modname* is
        given, use that module name to find attribute docs. If *check_module* is
        True, only generate if the object is defined in the module name it is
        imported from. If *all_members* is True, document all members.
        """
        if not self.parse_name():
            # need a module to import
            self.directive.warn(
                'don\'t know which module to import for autodocumenting '
                '%r (try placing a "module" or "currentmodule" directive '
                'in the document, or giving an explicit module name)'
                % self.name)
            return

        # now, import the module and get object to document
        if not self.import_object():
            return

        # If there is no real module defined, figure out which to use.
        # The real module is used in the module analyzer to look up the module
        # where the attribute documentation would actually be found in.
        # This is used for situations where you have a module that collects the
        # functions and classes of internal submodules.
        self.real_modname = real_modname or self.get_real_modname()

        # try to also get a source code analyzer for attribute docs
        try:
            self.analyzer = ModuleAnalyzer.for_module(self.real_modname)
            # parse right now, to get PycodeErrors on parsing (results will
            # be cached anyway)
            self.analyzer.find_attr_docs()
        except PycodeError as err:
            self.env.app.debug('[autodoc] module analyzer failed: %s', err)
            # no source file -- e.g. for builtin and C modules
            self.analyzer = None
            # at least add the module.__file__ as a dependency
            if hasattr(self.module, '__file__') and self.module.__file__:
                self.directive.filename_set.add(self.module.__file__)
        else:
            self.directive.filename_set.add(self.analyzer.srcname)

        # check __module__ of object (for members not given explicitly)
        if check_module:
            if not self.check_module():
                return

        sourcename = self.get_sourcename()

        # make sure that the result starts with an empty line.  This is
        # necessary for some situations where another directive preprocesses
        # reST and no starting newline is present
        self.add_line(u'', sourcename)

        # format the object's signature, if any
        sig = self.format_signature()

        # generate the directive header and options, if applicable
        self.add_directive_header(sig)
        self.add_line(u'', sourcename)

        # e.g. the module directive doesn't have content
        self.indent += self.content_indent

        # add all content (from docstrings, attribute docs etc.)
        self.add_content(more_content)

        # document members, if possible
        self.document_members(all_members)


class ModuleDocumenter(Documenter):
    """
    Specialized Documenter subclass for modules.
    """
    objtype = 'module'
    content_indent = u''
    titles_allowed = True

    option_spec = {
        'members': members_option, 'undoc-members': bool_option,
        'noindex': bool_option, 'inherited-members': bool_option,
        'show-inheritance': bool_option, 'synopsis': identity,
        'platform': identity, 'deprecated': bool_option,
        'member-order': identity, 'exclude-members': members_set_option,
        'private-members': bool_option, 'special-members': members_option,
        'imported-members': bool_option,
    }

    @classmethod
    def can_document_member(cls, member, membername, isattr, parent):
        # don't document submodules automatically
        return False

    def resolve_name(self, modname, parents, path, base):
        if modname is not None:
            self.directive.warn('"::" in automodule name doesn\'t make sense')
        return (path or '') + base, []

    def parse_name(self):
        ret = Documenter.parse_name(self)
        if self.args or self.retann:
            self.directive.warn('signature arguments or return annotation '
                                'given for automodule %s' % self.fullname)
        return ret

    def add_directive_header(self, sig):
        Documenter.add_directive_header(self, sig)

        sourcename = self.get_sourcename()

        # add some module-specific options
        if self.options.synopsis:
            self.add_line(
                u'   :synopsis: ' + self.options.synopsis, sourcename)
        if self.options.platform:
            self.add_line(
                u'   :platform: ' + self.options.platform, sourcename)
        if self.options.deprecated:
            self.add_line(u'   :deprecated:', sourcename)

    def get_object_members(self, want_all):
        if want_all:
            if not hasattr(self.object, '__all__'):
                # for implicit module members, check __module__ to avoid
                # documenting imported objects
                return True, safe_getmembers(self.object)
            else:
                memberlist = self.object.__all__
                # Sometimes __all__ is broken...
                if not isinstance(memberlist, (list, tuple)) or not \
                   all(isinstance(entry, string_types) for entry in memberlist):
                    self.directive.warn(
                        '__all__ should be a list of strings, not %r '
                        '(in module %s) -- ignoring __all__' %
                        (memberlist, self.fullname))
                    # fall back to all members
                    return True, safe_getmembers(self.object)
        else:
            memberlist = self.options.members or []
        ret = []
        for mname in memberlist:
            try:
                ret.append((mname, safe_getattr(self.object, mname)))
            except AttributeError:
                self.directive.warn(
                    'missing attribute mentioned in :members: or __all__: '
                    'module %s, attribute %s' % (
                        safe_getattr(self.object, '__name__', '???'), mname))
        return False, ret


class ModuleLevelDocumenter(Documenter):
    """
    Specialized Documenter subclass for objects on module level (functions,
    classes, data/constants).
    """
    def resolve_name(self, modname, parents, path, base):
        if modname is None:
            if path:
                modname = path.rstrip('.')
            else:
                # if documenting a toplevel object without explicit module,
                # it can be contained in another auto directive ...
                modname = self.env.temp_data.get('autodoc:module')
                # ... or in the scope of a module directive
                if not modname:
                    modname = self.env.ref_context.get('py:module')
                # ... else, it stays None, which means invalid
        return modname, parents + [base]


class ClassLevelDocumenter(Documenter):
    """
    Specialized Documenter subclass for objects on class level (methods,
    attributes).
    """
    def resolve_name(self, modname, parents, path, base):
        if modname is None:
            if path:
                mod_cls = path.rstrip('.')
            else:
                mod_cls = None
                # if documenting a class-level object without path,
                # there must be a current class, either from a parent
                # auto directive ...
                mod_cls = self.env.temp_data.get('autodoc:class')
                # ... or from a class directive
                if mod_cls is None:
                    mod_cls = self.env.ref_context.get('py:class')
                # ... if still None, there's no way to know
                if mod_cls is None:
                    return None, []
            modname, cls = rpartition(mod_cls, '.')
            parents = [cls]
            # if the module name is still missing, get it like above
            if not modname:
                modname = self.env.temp_data.get('autodoc:module')
            if not modname:
                modname = self.env.ref_context.get('py:module')
            # ... else, it stays None, which means invalid
        return modname, parents + [base]


class DocstringSignatureMixin(object):
    """
    Mixin for FunctionDocumenter and MethodDocumenter to provide the
    feature of reading the signature from the docstring.
    """

    def _find_signature(self, encoding=None):
        docstrings = self.get_doc(encoding)
        self._new_docstrings = docstrings[:]
        result = None

