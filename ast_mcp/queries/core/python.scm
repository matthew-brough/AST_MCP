; SPEC §V.8 — all patterns live here, none in .py
; Convention: one @def.<kind> per match, one @name.
; Patterns run specific -> general; extract.py keeps the first match per range.

(function_definition name: (identifier) @name) @def.function
(class_definition name: (identifier) @name) @def.class

(import_statement) @import.import
(import_from_statement) @import.from_import
