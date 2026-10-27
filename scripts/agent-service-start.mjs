// Executed ONLY as the agent uid, never by wizard-system/root. The image is
// immutable system wiring; the agent-owned JSON is just this process's config.
import { readFileSync } from 'node:fs'
import { setTimeout } from 'node:timers/promises'
let link
for (;;) {
  try {
    link = JSON.parse(readFileSync(process.env.WIZARD_PRIVSEP_LINK, 'utf8'))
    if (!link.hermes_key || !link.wam_token || link.wam_url !== 'http://127.0.0.1:7790') throw Error('incomplete link')
    break
  } catch { await setTimeout(2000) }
}
process.env.WIZARD_HERMES_API_KEY = link.hermes_key
process.env.WIZARD_MCP_TOKEN = link.mcp_token
process.env.WAM_URL = link.wam_url
process.env.WAM_TOKEN = link.wam_token
process.env.WIZARD_AGENT_BRIDGE = 'gateway'
// This Service is not an approval authority. wam's owner listener decides.
process.env.WIZARD_REQUIRE_CF_ACCESS = '0'
process.env.WIZARD_AUTH_MODE = 'none'
process.env.WIZARD_AGENTS = 'off'
await import('/app/server/index.mjs')
