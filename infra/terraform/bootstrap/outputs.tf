output "state_bucket_name" {
  description = "S3 bucket holding remote Terraform state for later stacks."
  value       = aws_s3_bucket.state.id
}
