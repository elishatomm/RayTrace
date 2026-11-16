# Deploying a shareable demo

The static recorded demo is live at **[raytrace-demo.elishatom04.workers.dev](https://raytrace-demo.elishatom04.workers.dev)** (Cloudflare Workers static assets, free). The live app is not deployed. Two kinds of link are possible, and both cost nothing on the hosts below. Free tiers change often: the facts here were read from each provider's own pages on 2026-11-16. Check again on the day you deploy.

| | Static recorded demo | Live app (public mode) |
|---|---|---|
| What runs | Only the visitor's browser. The real UI replays API answers recorded from a real run of the three flows. | The real server, in rules mode (or a hosted model if configured), with the three demo runs built into the image. |
| What a visitor can do | Click through Flows A, B and C. Nothing they do is saved. | Log in with a shared demo token, open the demo runs, diagnose, validate a fix, plan, approve, execute and open the receipt. |
| Cold start | None | About a minute after 15 idle minutes (Render free) |
| Best for | A link in an application form | A live walkthrough in an interview |

## 1. Static recorded demo (recommended for a link)

Build the site (needs Docker and npm; recording runs in a container with no network):

```console
$ npm --prefix frontend ci
$ scripts/demo/build-demo.sh ./demo-site
$ scripts/demo/test-demo.sh ./demo-site     # optional: checks it in headless Chromium
```

The folder holds about 23 files (about 11 MB), including a `_headers` file with the page's security headers. See [scripts/demo/README.md](../scripts/demo/README.md).

**Where to host it:**

- **Cloudflare Workers static assets** (what the live demo uses). On the dashboard's home page, drop the folder onto *Ship something new*. It serves `_headers`, and the address is `https://<worker-name>.<account-subdomain>.workers.dev`; rename the Worker under *Settings* to choose the first part.

- **Cloudflare Pages** (recommended). Free plan, static requests unmetered, serves `_headers`, and takes a drag-and-drop upload with no Git connection: *Workers & Pages → Create → Pages → Upload assets*. The address is `https://<project>.pages.dev`.
- **Netlify Drop**. Also drag-and-drop and reads `_headers`; the free plan's monthly credits are capped, and running out pauses every site on the account.
- **Hugging Face static Space**. Static Spaces are free for everyone. A *public* Space shows its files (here only the built site, which is public anyway); *private* shows the app only to you and invited collaborators; *protected* needs PRO. It does not serve `_headers`, so the CSP falls back to the page's own settings.

## 2. Live app on Render's free web service

Render's free web service is free for hobby use: 750 instance hours a month per workspace, spin-down after 15 minutes without traffic, about a minute to wake, and an ephemeral filesystem (fine here: public mode empties its data directory and copies the demo runs in at every start).

1. Create a demo token, and keep it secret:
   ```console
   $ python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Public mode refuses a weak token (see [RUNNING.md, Settings](RUNNING.md#settings)); if one is refused, generate another.
2. On Render: *New → Web Service*, connect the GitHub repository, choose the **Docker** runtime (it uses the repository's `Dockerfile`) and the **Free** instance type.
3. Set the environment variables:
   - `WORKBENCH_PUBLIC_ORIGIN` = `https://<service-name>.onrender.com` (the exact address Render shows);
   - `WORKBENCH_DEMO_TOKEN` = the token from step 1, as a secret;
   - optionally `WORKBENCH_MODEL_MODE=rules` to pin the rules baseline.
   Render sets `$PORT`, and the server listens on it.
4. Deploy, then check: `curl -sI https://<service-name>.onrender.com/health` should answer 200 with the security headers. Open the address, paste the token and pick an operator ID such as `op_reviewer`.
5. Open the link yourself 2–3 minutes before an interview so it is awake.

Whether Render's free tier accepts every Docker build is not stated on its free-tier page; if the build is refused, use the static demo.

**What public mode adds** (built and security-reviewed, see [SECURITY_AUDIT.md](SECURITY_AUDIT.md)): the server answers only its public origin, keeps sessions in a `__Host-` cookie (30 idle minutes, 8 hours at most, 20 at once), rate-limits state-changing requests, caps runs per session and per process, installs no webhook route, sends HSTS, and closes slow connections with deadlines.

## Hosts checked and not recommended

- **Hugging Face Docker Space**: creating one needs a paid plan (PRO, $9 a month). With PRO and *protected* visibility it would be the best fit: 2 vCPU, 16 GB, and the source stays private.
- **Koyeb**: its pricing page lists no free compute instance on 2026-11-16 (only a free Postgres tier).
- **Google Cloud Run, Fly.io, Railway**: need a card or have too small a free allowance for an always-on demo.
- **GitHub Pages**: cannot set response headers, and a free account publishes only from a public repository.

## Taking it down

- Cloudflare Pages: *Settings → Delete project*.
- Render: *Settings → Delete Web Service*, or suspend it.
- Rotate `WORKBENCH_DEMO_TOKEN` to cut off everyone who has the old one.
