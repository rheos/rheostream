# Connectors

Provider-specific source and destination adapters belong here. The generic signed
website receiver is implemented by `rheo_core.connectors` with HTTP mounting in
`rheo_app_core.connector_routes`, driven by module manifest bindings. Leads owns
connection lifecycle, mapping, receipts and processing. See the
[website setup](../modules/leads/README.md#signed-website-intake).
Import runners, email adapters, polling and external handoffs remain follow-up work.

Connectors own transport and provider translation, not domain policy. Credentials
and account-specific configuration stay private. Custom funnels should use a
generic contract without requiring a provider-specific branch in the core.
