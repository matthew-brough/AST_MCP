; Name is the block's labels joined: aws_s3_bucket.assets, or just `region`
; for a single-label block. Multiple @name captures are joined with "." by
; extract.py. The block keyword stays visible in the signature.
(block (identifier) (string_lit (template_literal) @name)+) @def.block
