if exists('g:loaded_deoplete_jedi')
  finish
endif
let g:loaded_deoplete_jedi = 1

if !exists("g:deoplete#sources#jedi#statement_length")
  let g:deoplete#sources#jedi#statement_length = get(g:, 'deoplete#sources#jedi#statement_length', 50)
endif

if !exists("g:deoplete#sources#jedi#enable_cache")
  let g:deoplete#sources#jedi#enable_cache = get(g:, 'deoplete#sources#jedi#enable_cache', 1)
endif

if !exists("g:deoplete#sources#jedi#debug_enabled")
  let g:deoplete#sources#jedi#debug_enabled = get(g:, 'deoplete#sources#jedi#debug_enabled', 0)
endif

if !exists("g:deoplete#sources#jedi#short_types")
  let g:deoplete#sources#jedi#short_types = get(g:, 'deoplete#sources#jedi#short_types', 0)
endif
