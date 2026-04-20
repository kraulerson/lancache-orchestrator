# Module: MCP Server
# Covers: Model Context Protocol servers, MCP tool providers, MCP resource servers,
#         AI assistant integrations, LLM-facing service layers

<!-- ENGINEER:CONTEXT -->
This project is an MCP (Model Context Protocol) server — a service that exposes tools, resources, and/or prompts to AI assistant clients like Claude Code. Evaluate it with the expectations of protocol-correct, schema-validated, transport-agnostic server software. MCP servers sit between AI assistants and external capabilities; failures manifest as silent hallucination triggers, not visible error pages. The engineering bar is higher than a typical API because the consumer is a language model that cannot debug a broken response.
<!-- /ENGINEER:CONTEXT -->

<!-- ENGINEER:CATEGORIES -->
8. **MCP Protocol Implementation**
   - Are tool definitions schema-valid with complete JSON Schema input specifications?
   - Are tool descriptions clear enough for an LLM client to invoke them correctly without examples?
   - Is input validation applied to every tool parameter before processing?
   - Are error responses structured and informative (not bare strings the LLM will misinterpret)?
   - Does the server handle malformed requests gracefully without crashing?
   - Are both stdio and HTTP/SSE transports supported (or is the transport limitation documented and justified)?
   - Is the MCP SDK used correctly, or is the protocol hand-rolled?

9. **Data Ingestion and Persistence**
   - Is the knowledge base or data store structured, versioned, and timestamped?
   - Is there a clear schema for stored data (not just raw JSON blobs)?
   - Are data updates atomic, or can partial writes corrupt the store?
   - Is there change tracking or diffing to detect what is new since the last check?
   - Is storage portable (SQLite, JSON files) or does it require a managed database server?
   - How does the data store handle concurrent reads during a write?

10. **External API Integration**
    - Are external API calls rate-limited to avoid bans or throttling?
    - Is there retry logic with exponential backoff for transient failures?
    - Are API responses cached with appropriate TTLs?
    - What happens when an external source is unreachable? Does the server degrade gracefully or fail entirely?
    - Are API keys and credentials stored securely (environment variables, not hardcoded)?
    - Is there a data freshness indicator so clients know how stale the information is?

11. **LLM Integration Quality** (if the server calls LLM APIs internally)
    - Is model selection justified (cost vs. capability for each use case)?
    - Are prompts structured to produce consistent, parseable output?
    - Is token usage tracked and bounded (no unbounded context stuffing)?
    - Are LLM responses validated before being returned to the MCP client?
    - Is there fallback behavior when the LLM API is unavailable or returns an error?
    - Are costs predictable? Is there a per-invocation or per-day cost ceiling?

12. **Operational Architecture**
    - If the server includes scheduled tasks (monitoring, polling), how is scheduling implemented?
    - Is there a health check mechanism (especially for long-running server processes)?
    - Can the server be restarted without data loss?
    - Is the server containerizable (Docker) for portable deployment?
    - Are logs structured and useful for debugging tool invocation failures?
    - Is the startup time reasonable for stdio transport (where the server is launched per-session)?
<!-- /ENGINEER:CATEGORIES -->

<!-- ENGINEER:OUTPUT -->
- A "Tool Definition Quality Matrix" evaluating each exposed tool for: schema completeness, description clarity, input validation, error handling, and response structure
- An "Operational Readiness Checklist" covering transport support, persistence integrity, monitoring, and deployment
<!-- /ENGINEER:OUTPUT -->

<!-- CIO:CONTEXT -->
This project is an MCP server — infrastructure that AI assistants depend on for extended capabilities. Evaluate it as a capability layer: cost profile (especially ongoing LLM API costs), operational burden, protocol maturity risk, and whether the capabilities justify a persistent service vs. simpler alternatives.
<!-- /CIO:CONTEXT -->

<!-- CIO:CATEGORIES -->
9. **API Cost and Token Economics**
   - What are the ongoing LLM API costs for internal analysis? Is cost per invocation documented?
   - How does cost scale with usage (more registered projects, more frequent monitoring)?
   - Are there external data source costs (API subscriptions, rate limit tiers)?
   - Is there a cost ceiling or budget control mechanism?
   - What is the total cost of ownership including hosting, storage, and API fees?

10. **Protocol and Ecosystem Risk**
    - MCP is a relatively new protocol. What is the migration path if the protocol changes significantly?
    - How many MCP clients actually support this server's features (stdio, SSE, resources)?
    - Is there vendor concentration risk from depending on a single protocol ecosystem?
    - What happens if the server is unavailable — do dependent AI workflows break entirely or degrade?
    - Is this providing value that a simpler approach (bookmarks, documentation, manual checks) cannot?
<!-- /CIO:CATEGORIES -->

<!-- CIO:OUTPUT -->
- A "Cost Model" projecting monthly operating costs at current and 5x usage
<!-- /CIO:OUTPUT -->

<!-- SECURITY:CONTEXT -->
This project is an MCP server that processes tool invocations from AI assistant clients, fetches data from external sources, and may call LLM APIs internally. Evaluate it as a multi-boundary system: the MCP transport boundary (client requests), the external API boundary (data ingestion), and the LLM boundary (internal analysis). Each boundary is a trust boundary where input can be adversarial.
<!-- /SECURITY:CONTEXT -->

<!-- SECURITY:CATEGORIES -->
10. **Tool Invocation Security**
    - Are all tool inputs validated against their JSON Schema before processing?
    - Can a malicious or confused client inject commands through tool parameters?
    - Is there parameter injection risk where tool inputs are interpolated into URLs, queries, or prompts?
    - Are tool responses bounded in size (prevent memory exhaustion from unbounded output)?
    - If tools accept file paths or URLs, are they validated against allowlists?

11. **External Data Source Trust**
    - Data scraped from external websites could contain adversarial content. Is scraped data sanitized?
    - Could poisoned documentation or blog posts inject instructions into the knowledge base that later influence LLM analysis?
    - Is there integrity verification for data sources (e.g., verifying the source is actually Anthropic's domain)?
    - Are HTTP redirects followed safely (no SSRF through redirect chains)?
    - Is TLS enforced for all external connections?

12. **LLM-Mediated Trust Boundary** (if the server calls LLM APIs internally)
    - Is user-provided project context sent to the LLM as-is, or is it sanitized?
    - Can project descriptions or metadata contain prompt injection that influences the LLM's advisory output?
    - Does the server leak sensitive project information (repo paths, internal URLs, credentials) to the LLM API?
    - Are LLM API keys stored and transmitted securely?
    - Is there data residency consideration for project context sent to external LLM APIs?

13. **Persistence Security**
    - Is the local data store (SQLite, JSON) protected against path traversal or injection?
    - Can one registered project's data leak into another project's audit results?
    - If the server stores API keys or tokens for external sources, are they encrypted at rest?
    - Is there access control on the MCP transport (who can invoke tools)?
<!-- /SECURITY:CATEGORIES -->

<!-- SECURITY:OUTPUT -->
- A **Trust Boundary Map** showing each boundary (MCP client, external APIs, LLM API, persistence) with data flows and threat vectors
- A **Tool Input Validation Assessment** covering each tool's parameter validation and injection risk
<!-- /SECURITY:OUTPUT -->

<!-- LEGAL:CONTEXT -->
This project is an MCP server that aggregates data from external sources (documentation websites, GitHub repositories, blog posts), processes it with LLM APIs, and provides advisory output. Evaluate legal risks around web scraping compliance, data aggregation, AI-generated advice liability, and the terms of service of data sources and API providers.
<!-- /LEGAL:CONTEXT -->

<!-- LEGAL:CATEGORIES -->
9. **Web Scraping and Data Aggregation Compliance**
   - Does the server respect robots.txt and terms of service of scraped websites?
   - Is the scraped content stored and redistributed in a way that could constitute copyright infringement?
   - Are there database rights (EU) or hot news doctrine (US) implications for aggregating real-time release information?
   - Does the scraping frequency and volume comply with the source's acceptable use policy?
   - Is there a DMCA/takedown mechanism if a data source objects to aggregation?

10. **LLM API Terms of Service Compliance**
    - Does sending project context (code descriptions, repo metadata) to the LLM API comply with the provider's terms?
    - Are there restrictions on using LLM output as automated advice or recommendations?
    - Does the LLM provider retain input/output data? What are the implications for project confidentiality?
    - If the server generates recommendations that a user follows and it causes damage, what is the liability chain?

11. **Advisory Output and Disclaimer Requirements**
    - The server provides technology recommendations. Could these be construed as professional consulting advice?
    - Is there adequate disclaimer that recommendations are AI-generated and should be independently verified?
    - If the server recommends a specific model or API and that recommendation leads to cost overruns, is there liability?
    - Does the server's advisory output create any implied warranty of fitness?
<!-- /LEGAL:CATEGORIES -->

<!-- LEGAL:OUTPUT -->
- A "Data Source Compliance Matrix" covering ToS, robots.txt, copyright, and acceptable use for each monitored source
- A "Recommendation Liability Assessment" evaluating disclaimer adequacy for AI-generated advice
<!-- /LEGAL:OUTPUT -->

<!-- TECHUSER:CONTEXT -->
This project is an MCP server that you would install and connect to your AI assistant (like Claude Code) to get capabilities it doesn't have natively. You are evaluating whether a technically literate non-coder can install, configure, connect, and benefit from this server without understanding MCP internals or TypeScript.
<!-- /TECHUSER:CONTEXT -->

<!-- TECHUSER:CATEGORIES -->
11. **Installation and MCP Client Connection**
    - Can you install the server with a single command or a short series of documented steps?
    - Is the Claude Code / MCP client configuration documented with copy-paste-ready JSON?
    - Can you verify the server is connected and working? Is there a test tool or verification step?
    - If installation fails, are error messages helpful enough to self-diagnose?
    - Is Docker an option for avoiding Node.js/TypeScript setup entirely?

12. **Day-to-Day Usability**
    - Are the tool names and descriptions intuitive enough that you know which tool to invoke?
    - Are the tool responses formatted for readability (not raw JSON dumps)?
    - Is the knowledge base useful without configuration, or does it require extensive setup before it provides value?
    - Can you register a project without understanding the full schema?
    - When the server gives recommendations, are they specific and actionable or vague and generic?

13. **Ongoing Maintenance**
    - Does the server update its knowledge base automatically, or do you need to trigger updates manually?
    - Is there a way to know if the server's data is stale?
    - When something breaks (API changes, external source unavailable), do you get a clear notification?
    - Can you update the server itself without losing your registered projects and configuration?
<!-- /TECHUSER:CATEGORIES -->

<!-- TECHUSER:OUTPUT -->
- A "Setup Difficulty Rating" evaluating installation, configuration, and verification for someone who has never configured an MCP server
- A "Value-to-Effort Matrix" comparing what you get from this server vs. manually checking Anthropic's docs and changelog
<!-- /TECHUSER:OUTPUT -->

<!-- REDTEAM:CONTEXT -->
This project is an MCP server that ingests data from external web sources, stores it in a local knowledge base, processes it with LLM APIs, and exposes tools to AI assistant clients. Prioritize: prompt injection through scraped content (adversarial documentation pages that influence the LLM's advisory output), tool parameter injection (client-provided inputs interpolated into URLs, database queries, or LLM prompts), SSRF through URL parameters in monitoring tools, data exfiltration through crafted project registrations that leak knowledge base contents, and persistence poisoning (corrupting the knowledge base to influence future recommendations). If the server supports HTTP/SSE transport, test for unauthenticated access and cross-origin abuse.
<!-- /REDTEAM:CONTEXT -->

<!-- REDTEAM:CATEGORIES -->
<!-- /REDTEAM:CATEGORIES -->

<!-- REDTEAM:OUTPUT -->
<!-- /REDTEAM:OUTPUT -->
