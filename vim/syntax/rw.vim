" Vim syntax file
" Language:     rw
" Maintainer:   rw project
" Description:  Syntax highlighting for the rw language
"               (Python-flavored, async-first, statically-typed compiled language)

if exists("b:current_syntax")
    finish
endif

" rw is case-sensitive.
syntax case match

" ---------------------------------------------------------------------
" Comments
" ---------------------------------------------------------------------
syntax match rwComment "#.*$" contains=rwTodo
syntax keyword rwTodo TODO FIXME XXX NOTE contained

" ---------------------------------------------------------------------
" String literals
" ---------------------------------------------------------------------
" Double-quoted strings with C-style escape sequences (\n \t \r \\ \" \0).
syntax region rwString start=+"+ end=+"+ skip=+\\\\\|\\"+ contains=rwStringEscape
syntax match  rwStringEscape +\\[ntr\\"0]+ contained

" ---------------------------------------------------------------------
" Numeric literals
" ---------------------------------------------------------------------
" Floats first (longer match wins). Integers exclude numbers that are part
" of a float (i.e. a digit followed by `.<digit>`).
syntax match rwFloat   "\<\d\+\.\d\+\>"
syntax match rwInteger "\<\d\+\>\%(\.\d\)\@!"

" ---------------------------------------------------------------------
" Keywords
" ---------------------------------------------------------------------
" Control flow / declarations.
syntax keyword rwKeyword def return if elif else while

" Boolean & logical keywords.
syntax keyword rwBoolean   true false
syntax keyword rwOperator  and or not

" Async keywords — highlighted distinctly so they stand out.
syntax keyword rwAsync spawn await

" Built-in print.
syntax keyword rwBuiltin print

" ---------------------------------------------------------------------
" Types
" ---------------------------------------------------------------------
syntax keyword rwType    int float bool string void
syntax keyword rwTypeCon Future

" ---------------------------------------------------------------------
" Reserved (not implemented in MVP, but tokenized).
" Highlighted as errors so users notice early.
" ---------------------------------------------------------------------
syntax keyword rwReserved extern class import for in as None

" ---------------------------------------------------------------------
" Function definition: highlight the function name after `def`.
" ---------------------------------------------------------------------
syntax match rwFunctionName "\<\h\w*\>" contained
syntax match rwFunctionDef  "\<def\>\s\+\h\w*" contains=rwKeyword,rwFunctionName

" Function call: identifier directly followed by `(`.
syntax match rwFunctionCall "\<\h\w*\>\ze\s*("

" ---------------------------------------------------------------------
" Operators / punctuation
" ---------------------------------------------------------------------
syntax match rwArrow      "->"
syntax match rwOperatorSym "[+\-*/%]\|==\|!=\|<=\|>=\|<\|>\|="

" ---------------------------------------------------------------------
" Default highlight links
" ---------------------------------------------------------------------
highlight default link rwComment       Comment
highlight default link rwTodo          Todo
highlight default link rwString        String
highlight default link rwStringEscape  SpecialChar
highlight default link rwInteger       Number
highlight default link rwFloat         Float
highlight default link rwBoolean       Boolean
highlight default link rwKeyword       Keyword
highlight default link rwOperator      Keyword
highlight default link rwOperatorSym   Operator
highlight default link rwArrow         Operator
highlight default link rwAsync         Statement
highlight default link rwBuiltin       Function
highlight default link rwType          Type
highlight default link rwTypeCon       Type
highlight default link rwReserved      Error
highlight default link rwFunctionName  Function
highlight default link rwFunctionCall  Function

let b:current_syntax = "rw"
