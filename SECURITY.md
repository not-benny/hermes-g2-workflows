# Security policy

`hermes-g2-workflows` is a capability-bearing MCP package. Treat any way to
forge, replay, widen, or bypass its exact profile, turn, workflow, argument,
package-digest, schema, cancellation, or receipt checks as a security issue.

Please use GitHub's private security-advisory reporting for the repository. Do
not include credentials, private profile files, device identifiers, reminder
text, calendar data, notifications, health data, or live gateway addresses in a
public issue.

The package is not a standalone glasses bridge. Its current security contract
requires the reviewed Hermes session-capability implementation, the separately
published Apache-2.0
[`hermes-g2-bridge`](https://github.com/not-benny/hermes-g2-bridge), a private
schema-pinned phone Device MCP, and an exact `even-g2` profile. The bridge
remains outside this package and its package digest.
