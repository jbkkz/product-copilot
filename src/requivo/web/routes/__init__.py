"""HTTP routes — thin adapters over the application services. A route parses the request, calls a
service, and renders a template. No business logic, no filesystem access, no `model.json` handling: all
of that lives in the Core and services."""
