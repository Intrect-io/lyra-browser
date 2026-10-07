import { Container, getContainer } from "@cloudflare/containers";

interface Env {
  LYRA_BROWSER: DurableObjectNamespace<LyraBrowserContainer>;
  // Shared Bearer token, set with `wrangler secret put LYRA_MCP_TOKEN`.
  LYRA_MCP_TOKEN?: string;
}

const ENV_PREFIX = "LYRA_BROWSER_";

export class LyraBrowserContainer extends Container<Env> {
  defaultPort = 8765;
  // Longer than the owner-idle (900 s) and consent-wait (300 s) windows, so a
  // person pausing mid-task does not lose the browser's tabs and logins.
  sleepAfter = "30m";
  pingEndpoint = "localhost/healthz";

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env);
    // Every LYRA_BROWSER_* string var on the Worker reaches the server, so
    // settings change without rebuilding the image. LYRA_MCP_TOKEN has another
    // prefix and stays in the Worker.
    const forwarded: Record<string, string> = {};
    for (const [name, value] of Object.entries(env as unknown as Record<string, unknown>)) {
      if (name.startsWith(ENV_PREFIX) && typeof value === "string") {
        forwarded[name] = value;
      }
    }
    this.envVars = forwarded;
  }
}

const encoder = new TextEncoder();

// Fail closed: with no token configured nobody is let in.
function authorized(request: Request, token: string | undefined): boolean {
  if (!token) return false;
  const header = request.headers.get("Authorization") ?? "";
  if (!header.startsWith("Bearer ")) return false;
  const given = encoder.encode(header.slice("Bearer ".length));
  const wanted = encoder.encode(token);
  // timingSafeEqual needs equal lengths; the length itself is not a secret
  // worth hiding for a random token.
  if (given.byteLength !== wanted.byteLength) return false;
  return crypto.subtle.timingSafeEqual(given, wanted);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const { pathname } = new URL(request.url);
    if (pathname !== "/mcp" && pathname !== "/mcp/") {
      return new Response("Not found", { status: 404 });
    }
    if (!authorized(request, env.LYRA_MCP_TOKEN)) {
      return new Response("Unauthorized", {
        status: 401,
        headers: { "WWW-Authenticate": 'Bearer realm="lyra-browser"' },
      });
    }
    // The server behind has no auth of its own; the credential stops here.
    const forward = new Request(request);
    forward.headers.delete("Authorization");
    // One name, one container: mcp-session-id state and the browser profile
    // live in a single instance.
    return getContainer(env.LYRA_BROWSER, "default").fetch(forward);
  },
} satisfies ExportedHandler<Env>;
