# Security policy

`hermes-g2-workflows` is a capability-bearing MCP package. Treat any way to
forge, replay, widen, or bypass its exact profile, turn, workflow, argument,
package-digest, schema, cancellation, or receipt checks as a security issue.

Please use GitHub's private security-advisory reporting for the repository. Do
not include credentials, private profile files, device identifiers, reminder
text, calendar data, notifications, health data, or live gateway addresses in a
public issue.

The package is not a standalone glasses bridge. Its current security contract
requires the reviewed Hermes session-capability implementation, an authenticated
native transport, a private schema-pinned phone Device MCP, and an exact
`even-g2` profile. The current native bridge is not part of this Apache-2.0
package and is not cleared for public redistribution.
