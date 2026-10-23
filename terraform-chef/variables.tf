variable "aws_region" {
  type        = string
  description = "AWS region for all resources."
  default     = "us-east-1"
}

variable "project_name" {
  type        = string
  description = "Project tag value applied to every resource."
  default     = "skripsi-iac"
}

variable "stack_name" {
  type        = string
  description = "Stack identifier used in name_prefix and tags. Must differ between the two stacks so they can run side-by-side."
  default     = "chef"
}

variable "vpc_cidr" {
  type        = string
  description = "CIDR block for the custom VPC."
  default     = "10.0.0.0/16"
}

variable "azs" {
  type        = list(string)
  description = "Availability zones to deploy into. Exactly two for this thesis."
  default     = ["us-east-1a", "us-east-1b"]
}

variable "key_pair_name" {
  type        = string
  description = "Existing EC2 key pair name in the Learner Lab account, used for SSH to the builder."
}

variable "private_key_path" {
  type        = string
  description = "Path to the matching private key file as seen from Windows (e.g. C:/Users/me/.ssh/lab.pem). Used by Terraform's remote-exec to gate on SSH readiness."
}

variable "builder_instance_type" {
  type        = string
  description = "EC2 instance type for the builder (AMI bake host)."
  default     = "t3.small"
}

variable "app_instance_type" {
  type        = string
  description = "EC2 instance type for ASG WordPress instances."
  default     = "t3.small"
}

variable "desired_capacity" {
  type        = number
  description = "Desired ASG size; also used as min_elb_capacity so apply blocks until healthy."
  default     = 2
}

variable "min_size" {
  type        = number
  description = "ASG minimum size."
  default     = 2
}

variable "max_size" {
  type        = number
  description = "ASG maximum size."
  default     = 4
}

variable "db_instance_class" {
  type        = string
  description = "RDS instance class (Learner Lab compatible)."
  default     = "db.t3.micro"
}

variable "db_allocated_storage" {
  type        = number
  description = "RDS allocated storage in GB."
  default     = 20
}

variable "db_name" {
  type        = string
  description = "WordPress database schema name."
  default     = "wordpress"
}

variable "db_username" {
  type        = string
  description = "RDS master username (also the WordPress DB user)."
  default     = "wpadmin"
}

variable "db_password" {
  type        = string
  description = "RDS master password. Passed through to wp-config.php via the configuration manager."
  sensitive   = true
}

variable "instance_profile_name" {
  type        = string
  description = "IAM instance profile name to attach to EC2 instances. Learner Lab provides LabRole / LabInstanceProfile."
  default     = "LabInstanceProfile"
}
