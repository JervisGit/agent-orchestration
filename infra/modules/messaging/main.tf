# Azure Service Bus namespace, queues, topics

variable "environment" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }

resource "azurerm_servicebus_namespace" "ao" {
  name                = "sb-ao-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = var.environment == "prod" ? "Standard" : "Basic"

  tags = var.tags
}

# Dead-letter topic for failed workflow steps
resource "azurerm_servicebus_topic" "dead_letter" {
  name                  = "ao-dead-letter"
  namespace_id          = azurerm_servicebus_namespace.ao.id
  max_size_in_megabytes = 1024
}

resource "azurerm_servicebus_subscription" "dead_letter_processor" {
  name               = "dead-letter-processor"
  topic_id           = azurerm_servicebus_topic.dead_letter.id
  max_delivery_count = 5
}

# Cross-workflow messaging topic
resource "azurerm_servicebus_topic" "workflow_events" {
  name                  = "ao-workflow-events"
  namespace_id          = azurerm_servicebus_namespace.ao.id
  max_size_in_megabytes = 1024
}

resource "azurerm_servicebus_subscription" "event_consumer" {
  name               = "event-consumer"
  topic_id           = azurerm_servicebus_topic.workflow_events.id
  max_delivery_count = 3
}

output "servicebus_namespace_id" {
  value = azurerm_servicebus_namespace.ao.id
}

output "servicebus_connection_string" {
  value     = azurerm_servicebus_namespace.ao.default_primary_connection_string
  sensitive = true
}
