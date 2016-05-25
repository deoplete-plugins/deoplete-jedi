# deoplete-jedi


[deoplete.nvim](https://github.com/Shougo/deoplete.nvim) source for [jedi](https://github.com/davidhalter/jedi).

|| **Status** |
|:---:|:---:|
| **Travis CI** |[![Build Status](https://travis-ci.org/zchee/deoplete-jedi.svg?branch=master)](https://travis-ci.org/zchee/deoplete-jedi)|
| **Gitter** |[![Join the chat at https://gitter.im/zchee/deoplete-jedi](https://badges.gitter.im/zchee/deoplete-jedi.svg)](https://gitter.im/zchee/deoplete-jedi?utm_source=badge&utm_medium=badge&utm_campaign=pr-badge&utm_content=badge)|


## Required

- Neovim and neovim/python-client
  - https://github.com/neovim/neovim
  - https://github.com/neovim/python-client

- deoplete.nvim
  - https://github.com/Shougo/deoplete.nvim

- jedi
  - https://github.com/davidhalter/jedi


## Install

```vim
NeoBundle 'zchee/deoplete-jedi'
# or
Plug 'zchee/deoplete-jedi'
```


## Options

- `g:deoplete#sources#jedi#statement_length`: Sets the maximum length of
  completion description text.  If this is exceeded, a simple description is
  used instead.  Default: `50`
- `deoplete#sources#jedi#enable_cache`: Enables caching of completions for
  faster results.  Default: `1`
- `deoplete#sources#jedi#show_docstring`: Shows docstring in preview window.
  Default: `0`
- `g:deoplete#sources#jedi#python_path`: Set the Python interpreter path to use
  for the completion server.  deoplete-jedi uses the first available `python`
  in `$PATH`.  Use this only if you want use a specific Python interpreter.
  This has no effect if `$VIRTUAL_ENV` is present in the environment.
  **Note**: This is completely unrelated to configuring Neovim.


## Virtual Environments

If you are using virtualenv, it is recommended that you create environments
specifically for Neovim.  This way, you will not need to install the neovim
package in each virtualenv.  Once you have created them, add the following to
your vimrc file:

```vim
let g:python_host_prog = '/full/path/to/neovim2/bin/python'
let g:python3_host_prog = '/full/path/to/neovim3/bin/python'
```

Deoplete only requires Python 3.  See `:h nvim-python-quickstart` for more
information.
