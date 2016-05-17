if exists('g:loaded_deoplete_jedi')
  finish
endif
let g:loaded_deoplete_jedi = 1


let g:deoplete#sources#go#align_class =
      \ get( g:, 'deoplete#sources#go#align_class', 0 )

let g:deoplete#sources#jedi#short_types =
      \ get(g:, 'deoplete#sources#jedi#short_types', 0)

let g:deoplete#sources#jedi#statement_length =
      \ get(g:, 'deoplete#sources#jedi#statement_length', 0)

let g:deoplete#sources#jedi#debug_enabled =
      \ get(g:, 'deoplete#sources#jedi#debug_enabled', 0)

let g:deoplete#sources#jedi#show_docstring =
      \ get(g:, 'deoplete#sources#jedi#show_docstring', 0)

" Only one worker is really needed since deoplete-jedi has a pretty aggressive
" cache.  Two workers may be needed if working with very large source files.
let g:deoplete#sources#jedi#worker_threads =
      \ get(g:, 'deoplete#sources#jedi#worker_threads', 1)

" Hard coded python interpreter location
let g:deoplete#sources#jedi#python_path =
      \ get(g:, 'deoplete#sources#jedi#python_path', '')
