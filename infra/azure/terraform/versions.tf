terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.13" # checked against the 5.13.0 docs (2026-11-05)
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }

  # State lives in an Azure Storage account created once by bootstrap.sh (see README.md):
  #   terraform init -backend-config=environments/prod.backend.hcl
  backend "azurerm" {}
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy = false # never hard-delete secrets by accident
    }
    resource_group {
      prevent_deletion_if_contains_resources = true
    }
  }
  subscription_id     = var.subscription_id
  storage_use_azuread = true
}
