// Checks a built recorded-demo site before build-demo.sh hands it over (Node, standard library only):
//   * _headers sends exactly the security headers the API sent while recording (recording-summary.json);
//   * index.html carries the same Content-Security-Policy in a meta element, minus frame-ancestors (which a meta policy
//     cannot carry), and no inline script, inline style or reference to another origin; the CSS imports nothing;
//   * every file the manifest names is there;
//   * no file is over 5 MB and the whole site is under 25 MB;
//   * no file holds a local path, user name or host name (the --forbid values, and common home and temp folders).
// Usage: node check-site.mjs --site <dir> --summary <recording-summary.json> [--forbid <text>]...
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

const MAX_FILE_BYTES = 5_000_000;
const MAX_SITE_BYTES = 25_000_000;
const LOCAL_PATH_MARKERS = ["/Users/", "/home/", "/private/var/", "/var/folders/", "C:\\Users\\"];
const SECURITY_HEADERS = ["Content-Security-Policy", "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy"];

function parseArguments(argv) {
  const options = { forbid: [] };
  for (let index = 0; index < argv.length; index += 2) {
    const [name, value] = [argv[index], argv[index + 1]];
    if (value === undefined) {
      throw new Error(`${name} needs a value`);
    }
    if (name === "--forbid") {
      options.forbid.push(value);
    } else if (name === "--site" || name === "--summary") {
      options[name.slice(2)] = value;
    } else {
      throw new Error(`unknown option ${name}`);
    }
  }
  if (!options.site || !options.summary) {
    throw new Error("usage: node check-site.mjs --site <dir> --summary <recording-summary.json> [--forbid <text>]...");
  }
  return options;
}

function filesUnder(root) {
  return readdirSync(root, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => path.join(entry.parentPath, entry.name));
}

// The headers of the rule for /* in a Cloudflare Pages / Netlify _headers file.
function headersForAllPaths(text) {
  const headers = {};
  let inAllPaths = false;
  for (const line of text.split("\n")) {
    if (line.trim() === "" || line.trim().startsWith("#")) {
      continue;
    }
    if (!/^\s/.test(line)) {
      inAllPaths = line.trim() === "/*";
      continue;
    }
    if (inAllPaths) {
      const separator = line.indexOf(":");
      headers[line.slice(0, separator).trim()] = line.slice(separator + 1).trim();
    }
  }
  return headers;
}

function checkHeaders(site, recorded) {
  const sent = headersForAllPaths(readFileSync(path.join(site, "_headers"), "utf8"));
  return SECURITY_HEADERS.filter((name) => sent[name] !== recorded[name.toLowerCase()])
    .map((name) => `_headers sends ${name}: ${sent[name]}, the API sent ${recorded[name.toLowerCase()]}`);
}

function checkPage(site, recorded) {
  const problems = [];
  const html = readFileSync(path.join(site, "index.html"), "utf8");
  const metaPolicy = /<meta http-equiv="Content-Security-Policy" content="([^"]*)"/.exec(html)?.[1];
  const wantedMeta = recorded["content-security-policy"].split("; ").filter((part) => !part.startsWith("frame-ancestors")).join("; ");
  if (metaPolicy !== wantedMeta) {
    problems.push(`index.html's meta policy is ${metaPolicy}, expected ${wantedMeta}`);
  }
  if (!html.includes(`<meta name="referrer" content="${recorded["referrer-policy"]}"`)) {
    problems.push("index.html lacks the API's Referrer-Policy as a meta element");
  }
  const inline = [
    [/<script(?![^>]*\bsrc=)[^>]*>/i, "an inline script"],
    [/<style[\s>]/i, "a style element"],
    [/\sstyle=/i, "a style attribute"],
    [/\s(?:src|href)="(?!\.\/|data:)[^"]*"/i, "a reference that is not to the site's own files"],
  ];
  return [...problems, ...inline.filter(([pattern]) => pattern.test(html)).map(([, what]) => `index.html has ${what}`)];
}

function checkStyles(site, files) {
  return files.filter((file) => file.endsWith(".css"))
    .filter((file) => /@import|url\(\s*["']?(?:https?:)?\/\//i.test(readFileSync(file, "utf8")))
    .map((file) => `${path.relative(site, file)} imports from another origin`);
}

function checkManifestFiles(site, files) {
  const manifest = JSON.parse(readFileSync(path.join(site, "demo-data", "manifest.json"), "utf8"));
  const named = manifest.runs.flatMap((run) => [
    ...run.stages.map((stage) => stage.file),
    ...run.receipts.flatMap((receipt) => [receipt.json, receipt.markdown]),
  ]);
  return named.filter((name) => !files.includes(path.join(site, name)))
    .map((name) => `the manifest names ${name}, which the site lacks`);
}

function measure(site, files) {
  const sizes = files.map((file) => ({ name: path.relative(site, file), bytes: statSync(file).size }));
  const total = sizes.reduce((sum, item) => sum + item.bytes, 0);
  const largest = sizes.reduce((best, item) => (item.bytes > best.bytes ? item : best), { name: "", bytes: 0 });
  const problems = sizes.filter((item) => item.bytes > MAX_FILE_BYTES)
    .map((item) => `${item.name} is ${item.bytes} bytes, over ${MAX_FILE_BYTES}`);
  if (total > MAX_SITE_BYTES) {
    problems.push(`the site is ${total} bytes, over ${MAX_SITE_BYTES}`);
  }
  return { total, largest, problems };
}

function checkLocalNames(site, files, given) {
  const forbidden = [
    ...LOCAL_PATH_MARKERS.map((marker) => ({ marker, named: `the local path marker ${marker}` })),
    ...given.map((marker, index) => ({ marker, named: `--forbid value ${index + 1}` })),
  ];
  return files.flatMap((file) => {
    const text = readFileSync(file, "latin1");
    return forbidden.filter(({ marker }) => text.includes(marker))
      .map(({ named }) => `${path.relative(site, file)} contains ${named}`);
  });
}

function main() {
  const options = parseArguments(process.argv.slice(2));
  const site = path.resolve(options.site);
  const recorded = JSON.parse(readFileSync(options.summary, "utf8")).security_headers;
  const files = filesUnder(site);
  const given = options.forbid.filter((text) => text.length >= 4);
  const sizes = measure(site, files);
  const problems = [
    ...checkHeaders(site, recorded),
    ...checkPage(site, recorded),
    ...checkStyles(site, files),
    ...checkManifestFiles(site, files),
    ...sizes.problems,
    ...checkLocalNames(site, files, given),
  ];
  console.log(JSON.stringify({ files: files.length, bytes: sizes.total, largest: sizes.largest,
    forbid_checked: given.length, forbid_skipped_as_too_short: options.forbid.length - given.length }));
  if (problems.length > 0) {
    console.error(`[check-site] ${problems.length} problem(s):\n  ${problems.join("\n  ")}`);
    process.exit(1);
  }
  console.log("[check-site] headers, meta policy, inline content, origins, files, sizes and local names: all as expected");
}

main();
