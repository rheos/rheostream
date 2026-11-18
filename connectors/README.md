# Connectors

Provider-specific source and destination adapters belong here; the `rheo_connectors`
package is still an empty placeholder. The connector code that runs today lives in core
and the modules:

- The generic signed website receiver is implemented by `rheo_core.connectors` with
  HTTP mounting in `rheo_app_core.connector_routes`, driven by module manifest bindings.
  Leads owns connection lifecycle, mapping, receipts and processing. See the
  [website setup](../modules/leads/README.md#signed-website-intake).
- For a remote MCP source, core has an OAuth client (`rheo_core.connectors.oauth`) and
  an allowlisted, read-only MCP client (`rheo_core.connectors.mcp_client`), both sending
  through `rheo_core.connectors.egress`. Leads' job feed collector
  (`modules/leads/src/rheo_leads/jobfeed/`) uses them to poll a job provider every two
  hours. See the [job feed](../modules/leads/README.md#job-feed) and its
  [operator settings](../deploy/README.md#job-feed-collector).

CSV and JSON import runners, email adapters and destinations outside rheoStream remain
follow-up work.

Connectors own transport and provider translation, not domain policy. Credentials
and account-specific configuration stay private. Custom funnels should use a
generic contract without requiring a provider-specific branch in the core.
