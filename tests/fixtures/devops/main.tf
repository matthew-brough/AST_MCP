# Primary bucket.
resource "aws_s3_bucket" "assets" {
  bucket = "my-assets"
}

variable "region" {
  type = string
}

module "network" {
  source = "./network"
}

output "bucket_name" {
  value = aws_s3_bucket.assets.bucket
}
