terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region  = "eu-central-1"
  profile = "personal"
}

# --- SSH Key Pair ---
# Uses your existing ~/.ssh/id_ed25519.pub (or id_rsa.pub).
# If you don't have one, run: ssh-keygen -t ed25519
resource "aws_key_pair" "agent" {
  key_name   = "playwright-agent-key"
  public_key = file("~/.ssh/id_ed25519.pub")
}

# --- Security Group (SSH only) ---
resource "aws_security_group" "agent" {
  name        = "playwright-agent-sg"
  description = "Allow SSH inbound"

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "playwright-agent-sg"
  }
}

# --- EC2 Instance ---
# t3.micro: 2 vCPU, 1 GB RAM — Free Tier eligible, smallest practical size
# for running headless Chromium.
resource "aws_instance" "agent" {
  ami           = "ami-0a628e1e89aaedf80" # Amazon Linux 2023, eu-central-1
  instance_type = "t3.micro"

  key_name               = aws_key_pair.agent.key_name
  vpc_security_group_ids = [aws_security_group.agent.id]

  # 8 GB gp3 root volume (Free Tier covers up to 30 GB)
  root_block_device {
    volume_size = 8
    volume_type = "gp3"
  }

  tags = {
    Name = "playwright-agent"
  }
}

# --- Outputs ---
output "instance_id" {
  value = aws_instance.agent.id
}

output "public_ip" {
  value = aws_instance.agent.public_ip
}

output "ssh_command" {
  value = "ssh -i ~/.ssh/id_ed25519 ec2-user@${aws_instance.agent.public_ip}"
}
