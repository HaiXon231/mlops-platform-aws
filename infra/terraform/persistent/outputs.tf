output "bucket_names" {
  description = "Persistent S3 bucket names by purpose."
  value       = { for purpose, bucket in aws_s3_bucket.persistent : purpose => bucket.id }
}

output "ecr_repository_urls" {
  description = "Private ECR repository URLs by image purpose."
  value       = { for purpose, repository in aws_ecr_repository.images : purpose => repository.repository_url }
}
