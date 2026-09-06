; A named build stage. FROM without AS yields no symbol, only an import.
(from_instruction (image_alias) @name) @def.stage

(from_instruction (image_spec) @import.module) @import.base_image
(copy_instruction (param) @import.module) @import.copy_from
