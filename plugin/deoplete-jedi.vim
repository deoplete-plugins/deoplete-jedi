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
