#!/usr/bin/env node
// Daily upstream check: is there a newer stable upstream release than the one
// this package pins? Settings live in .github/upstream-check.json.
//
//   node .github/scripts/upstream-check.mjs            dry run: prints what it finds, changes nothing
//   node .github/scripts/upstream-check.mjs --write    also updates the pins, the checksums and the
//                                                      package version, and writes the PR text
//
// Only stable releases count: drafts, GitHub pre-releases and tags such as
// -rc.1, b3 or -nightly are skipped. A release whose image (or download) is
// not published yet is skipped until it is. No dependencies; Node 18 or newer.
// GITHUB_TOKEN (optional locally) raises the GitHub API rate limit.

import { readFileSync, writeFileSync, appendFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";

const args = process.argv.slice(2);
const WRITE = args.includes("--write");
const opt = (name, dflt) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : dflt;
};
const CONFIG = opt("--config", ".github/upstream-check.json");
const SUMMARY = opt("--summary", process.env.RUNNER_TEMP ? join(process.env.RUNNER_TEMP, "upstream-check.md") : "");
const DEFAULT_TAG = "^v?(\\d+(?:\\.\\d+)+)$";
const UA = { "user-agent": "avado-upstream-check" };

const config = JSON.parse(readFileSync(CONFIG, "utf8"));
const files = new Map(); // path -> current text (edited in memory, written at the end)
const text = (path) => {
  if (!files.has(path)) files.set(path, readFileSync(path, "utf8"));
  return files.get(path);
};

// ---------- versions

const parts = (v) => v.split(".").map(Number);
function cmp(a, b) {
  const x = parts(a), y = parts(b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const d = (x[i] ?? 0) - (y[i] ?? 0);
    if (d) return d;
  }
  return 0;
}
// "Bigger" = the first number changes, or the second one while the first is 0 (0.x releases).
function bigger(from, to) {
  const x = parts(from), y = parts(to);
  return x[0] !== y[0] || (x[0] === 0 && x[1] !== y[1]);
}

// ---------- pins: text templates with {version} and {name} placeholders

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
function pinRegex(template) {
  const names = [];
  let src = "";
  for (const piece of template.split(/(\{[a-z0-9_]+\})/i)) {
    const m = piece.match(/^\{([a-z0-9_]+)\}$/i);
    if (!m) { src += esc(piece); continue; }
    const name = m[1];
    if (names.includes(name)) { src += `\\k<${name}>`; continue; }
    names.push(name);
    src += `(?<${name}>${name === "version" ? "\\d+(?:\\.\\d+)*" : "[0-9a-f]{64}"})`;
  }
  return new RegExp(src, "g");
}
const fill = (template, values) =>
  template.replace(/\{([a-z0-9_]+)\}/gi, (_, n) => {
    if (values[n] === undefined) throw new Error(`no value for {${n}} in "${template}"`);
    return values[n];
  });

// Reads the pinned values of one component and checks that every pin agrees.
function readPins(c) {
  const now = {};
  for (const pin of c.pins) {
    const matches = [...text(pin.file).matchAll(pinRegex(pin.text))];
    if (!matches.length) {
      if (pin.optional) continue;
      throw new Error(`${c.name}: "${pin.text}" not found in ${pin.file}. Fix .github/upstream-check.json or the file.`);
    }
    for (const m of matches) {
      for (const [k, v] of Object.entries(m.groups ?? {})) {
        if (now[k] !== undefined && now[k] !== v) {
          throw new Error(`${c.name}: ${pin.file} has {${k}} = ${v}, another pin has ${now[k]}. Make them agree first.`);
        }
        now[k] = v;
      }
    }
  }
  if (!now.version) throw new Error(`${c.name}: no pin with {version} matched`);
  return now;
}

function writePins(c, values) {
  for (const pin of c.pins) {
    const before = text(pin.file);
    files.set(pin.file, before.replace(pinRegex(pin.text), () => fill(pin.text, values)));
  }
}

// ---------- http

async function get(url, { headers = {}, method = "GET", ok404 = false } = {}) {
  for (let attempt = 1; ; attempt++) {
    let res;
    try {
      res = await fetch(url, { method, headers: { ...UA, ...headers }, redirect: "follow" });
    } catch (e) {
      if (attempt < 3) { await new Promise((r) => setTimeout(r, 3000 * attempt)); continue; }
      throw new Error(`${url}: ${e.message}`);
    }
    if (res.status === 404 && ok404) return null;
    if (res.status >= 500 && attempt < 3) { await new Promise((r) => setTimeout(r, 3000 * attempt)); continue; }
    return res;
  }
}

async function github(path) {
  const headers = { accept: "application/vnd.github+json" };
  if (process.env.GITHUB_TOKEN) headers.authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
  const res = await get(`https://api.github.com/${path}`, { headers });
  if (!res.ok) throw new Error(`GitHub ${path}: HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
  return res.json();
}

// Stable versions from GitHub releases (default) or from plain git tags.
async function upstreamVersions(c) {
  const tagRe = new RegExp(c.tag ?? DEFAULT_TAG);
  const out = new Map();
  for (let page = 1; page <= 3; page++) {
    const list = await github(`repos/${c.github}/${c.source === "tags" ? "tags" : "releases"}?per_page=100&page=${page}`);
    for (const r of list) {
      const tag = c.source === "tags" ? r.name : r.tag_name;
      if (c.source !== "tags" && (r.draft || r.prerelease)) continue;
      const m = tag.match(tagRe);
      if (!m || out.has(m[1])) continue;
      out.set(m[1], {
        version: m[1],
        tag,
        notes: c.source === "tags" ? `https://github.com/${c.github}/releases/tag/${tag}` : r.html_url,
      });
    }
    if (list.length < 100) break;
  }
  return [...out.values()].sort((a, b) => cmp(b.version, a.version));
}

// ---------- container registries (Docker Hub, ghcr.io, any v2 registry with anonymous pull)

const ACCEPT = [
  "application/vnd.oci.image.index.v1+json",
  "application/vnd.docker.distribution.manifest.list.v2+json",
  "application/vnd.oci.image.manifest.v1+json",
  "application/vnd.docker.distribution.manifest.v2+json",
].join(", ");
const tokens = new Map();

function parseRef(ref) {
  const at = ref.lastIndexOf(":");
  let name = ref.slice(0, at), tag = ref.slice(at + 1);
  let host = "registry-1.docker.io";
  const first = name.split("/")[0];
  if (name.includes("/") && (first.includes(".") || first.includes(":"))) {
    host = first; name = name.slice(first.length + 1);
  } else if (!name.includes("/")) name = `library/${name}`;
  return { host, name, tag };
}

async function registry(host, name, path, method) {
  const url = `https://${host}/v2/${name}/${path}`;
  const key = `${host}/${name}`;
  const headers = { accept: ACCEPT };
  if (tokens.has(key)) headers.authorization = `Bearer ${tokens.get(key)}`;
  let res = await get(url, { method, headers });
  if (res.status === 401) {
    const h = res.headers.get("www-authenticate") ?? "";
    const p = Object.fromEntries([...h.matchAll(/(\w+)="([^"]*)"/g)].map((m) => [m[1], m[2]]));
    if (!p.realm) throw new Error(`${url}: 401 without a token address`);
    const q = new URLSearchParams({ scope: p.scope ?? `repository:${name}:pull` });
    if (p.service) q.set("service", p.service);
    const t = await (await get(`${p.realm}?${q}`)).json();
    tokens.set(key, t.token ?? t.access_token);
    headers.authorization = `Bearer ${tokens.get(key)}`;
    res = await get(url, { method, headers });
  }
  return res;
}

// The digest a FROM line needs (the multi-arch index when there is one), after
// checking that the image has a linux/amd64 build. null when the tag does not exist.
async function imageDigest(ref) {
  const { host, name, tag } = parseRef(ref);
  const res = await registry(host, name, `manifests/${tag}`, "GET");
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${ref}: registry HTTP ${res.status}`);
  const body = Buffer.from(await res.arrayBuffer());
  const digest = res.headers.get("docker-content-digest") ?? `sha256:${createHash("sha256").update(body).digest("hex")}`;
  const manifest = JSON.parse(body.toString("utf8"));
  if (manifest.manifests) {
    const amd64 = manifest.manifests.some((m) => m.platform?.os === "linux" && m.platform?.architecture === "amd64");
    if (!amd64) throw new Error(`${ref}: no linux/amd64 build in the image`);
  }
  return digest.replace(/^sha256:/, "");
}

// ---------- downloads checked against a signed SHA256SUMS (Bitcoin Core)

async function sha256sums(spec, values) {
  const url = fill(spec.sums, values);
  const res = await get(url, { ok404: true });
  if (!res) return null;
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  const sums = await res.text();
  const file = fill(spec.file, values);
  const line = sums.split("\n").find((l) => l.trim().split(/\s+\*?/)[1] === file);
  if (!line) return null; // listed later, or not built for this platform
  const hash = line.trim().split(/\s+/)[0].toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(hash)) throw new Error(`${url}: bad line for ${file}`);
  let signers = null;
  if (spec.gpg) signers = await verifySigned(url, sums, spec.gpg);
  return { hash, signers };
}

async function verifySigned(url, sums, g) {
  const sigRes = await get(`${url}.asc`, { ok404: true });
  if (!sigRes || !sigRes.ok) throw new Error(`${url}.asc: no signature file`);
  const home = mkdtempSync(join(tmpdir(), "upstream-gpg-"));
  try {
    writeFileSync(join(home, "SUMS"), sums);
    writeFileSync(join(home, "SUMS.asc"), Buffer.from(await sigRes.arrayBuffer()));
    const keys = await github(`repos/${g.keys}/contents/${g.path}`);
    for (const k of keys.filter((k) => k.type === "file" && /\.(gpg|asc)$/.test(k.name))) {
      const r = await get(k.download_url);
      if (!r.ok) continue;
      writeFileSync(join(home, "key"), Buffer.from(await r.arrayBuffer()));
      try {
        execFileSync("gpg", ["--homedir", home, "--batch", "--quiet", "--import", join(home, "key")], { stdio: "ignore" });
      } catch { /* an expired or broken key file only lowers the count */ }
    }
    let status = "";
    try {
      status = execFileSync("gpg", ["--homedir", home, "--batch", "--status-fd", "1", "--verify", join(home, "SUMS.asc"), join(home, "SUMS")], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
    } catch (e) {
      status = e.stdout?.toString() ?? ""; // non-zero when some signers' keys are not in the list
    }
    if (/^\[GNUPG:\] BADSIG/m.test(status)) throw new Error(`${url}: a BAD signature, stopping`);
    const good = [...status.matchAll(/^\[GNUPG:\] GOODSIG \S+ (.+)$/gm)].map((m) => m[1].replace(/\s*<.*>$/, "").trim());
    if (good.length < g.min) throw new Error(`${url}: only ${good.length} good signatures, need ${g.min}`);
    return good;
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
}

// ---------- one component

// Resolves everything a version needs (image digests, checksums).
// Returns null when something is not published yet.
async function resolve(c, version) {
  const values = { version };
  const notes = [];
  for (const img of c.images ?? []) {
    const ref = fill(img.ref, values);
    const d = await imageDigest(ref);
    if (!d) return { missing: ref };
    values[img.var] = d;
    notes.push(`\`${ref}\` has a linux/amd64 build; pinned to digest \`sha256:${d.slice(0, 12)}…\``);
  }
  for (const s of c.checksums ?? []) {
    const r = await sha256sums(s, values);
    if (!r) return { missing: fill(s.file, values) };
    values[s.var] = r.hash;
    notes.push(`\`${fill(s.file, values)}\`: SHA-256 \`${r.hash.slice(0, 12)}…\` from SHA256SUMS` +
      (r.signers ? `, signed by ${r.signers.length} builders (${r.signers.join(", ")})` : ""));
  }
  return { values, notes };
}

async function check(c) {
  const now = readPins(c);
  const result = { c, now: now.version, warnings: [] };

  // The pinned digests should still be what the registry serves for the pinned tag.
  const current = await resolve(c, now.version).catch((e) => ({ error: e.message }));
  if (current.error) result.warnings.push(`could not re-check the pinned version: ${current.error}`);
  else if (current.missing) result.warnings.push(`the pinned \`${current.missing}\` is not found upstream any more`);
  else {
    result.pinned = current.notes;
    for (const [k, v] of Object.entries(current.values)) {
      if (k !== "version" && now[k] && now[k] !== v) {
        result.warnings.push(`pinned {${k}} ${now[k].slice(0, 12)}… differs from upstream's ${v.slice(0, 12)}… for ${now.version}`);
      }
    }
  }

  const releases = await upstreamVersions(c);
  result.latest = releases[0]?.version;
  for (const rel of releases.filter((r) => cmp(r.version, now.version) > 0)) {
    const r = await resolve(c, rel.version);
    if (r.missing) {
      result.warnings.push(`${rel.version} is released, but \`${r.missing}\` is not published yet; waiting for it`);
      continue;
    }
    Object.assign(result, { next: rel, values: r.values, notes: r.notes });
    break;
  }
  return result;
}

// ---------- package version (patch bump)

function bumpPackage() {
  const manifest = text("dappnode_package.json");
  const m = manifest.match(/"version":\s*"(\d+)\.(\d+)\.(\d+)"/);
  if (!m) throw new Error("dappnode_package.json: no version");
  const from = `${m[1]}.${m[2]}.${m[3]}`;
  const to = `${m[1]}.${m[2]}.${Number(m[3]) + 1}`;
  const name = JSON.parse(manifest).name;
  files.set("dappnode_package.json", manifest.replace(m[0], m[0].replace(from, to)));
  const compose = text("docker-compose.yml");
  const image = `${name}:${from}`;
  if (!compose.includes(image)) throw new Error(`docker-compose.yml: image ${image} not found`);
  files.set("docker-compose.yml", compose.split(image).join(`${name}:${to}`));
  return { from, to };
}

// ---------- main

const results = [];
for (const c of config.components) results.push(await check(c));

for (const r of results) {
  const state = r.next ? `UPDATE AVAILABLE ${r.now} -> ${r.next.version} (${r.next.notes})` : `up to date (${r.now}; newest stable upstream: ${r.latest ?? "none found"})`;
  console.log(`${r.c.name}: ${state}`);
  for (const n of r.pinned ?? []) console.log(`  pinned ${r.now}: ${n.replace(/`/g, "").replace("pinned to digest", "digest")}`);
  for (const n of r.notes ?? []) console.log(`  new ${r.next.version}: ${n.replace(/`/g, "")}`);
  for (const w of r.warnings) console.log(`  note: ${w.replace(/`/g, "")}`);
}

const updates = results.filter((r) => r.next);
if (!updates.length) {
  console.log("All up to date. Nothing to change.");
  // The PR step still runs: with no change it opens nothing and removes a leftover branch.
  if (WRITE && SUMMARY) writeFileSync(SUMMARY, `${config.title}: all up to date.\n`);
  if (WRITE && process.env.GITHUB_OUTPUT) appendFileSync(process.env.GITHUB_OUTPUT, "changed=false\ntitle=Upstream check\n");
  process.exit(0);
}
if (!WRITE) {
  console.log("Dry run: nothing written. Run with --write to update the pins.");
  process.exit(0);
}

for (const r of updates) writePins(r.c, r.values);
const pkg = bumpPackage();
for (const [path, body] of files) if (body !== readFileSync(path, "utf8")) writeFileSync(path, body);

const label = updates.map((r) => `${r.c.name} ${r.next.version}`).join(", ");
const title = `Update ${label}`;
const readFirst = [
  ...updates.filter((r) => bigger(r.now, r.next.version)).map((r) => `- **Bigger update:** ${r.c.name} ${r.now} → ${r.next.version} is a new major version. Its release notes may list changes that need work in this package.`),
  ...updates.filter((r) => r.c.caution).map((r) => `- **${r.c.name}:** ${r.c.caution}`),
];
const md = [
  `A new stable upstream version is out for **${config.title}**. This PR updates the package to it.`,
  "",
  "| Part | Now | New | Release notes |",
  "|---|---|---|---|",
  ...updates.map((r) => `| ${r.c.name} | ${r.now} | **${r.next.version}** | [${r.next.tag}](${r.next.notes})${r.c.notes ? ` · [more](${fill(r.c.notes, { version: r.next.version })})` : ""} |`),
  "",
  `Package version: ${pkg.from} → **${pkg.to}**.`,
  "",
  ...(readFirst.length ? ["**Read first**", ...readFirst, ""] : []),
  "**Checked by the daily upstream check**",
  ...updates.flatMap((r) => r.notes.map((n) => `- ${n}`)),
  ...results.flatMap((r) => r.warnings.map((w) => `- Note (${r.c.name}): ${w}`)),
  "",
  "**What happens next**",
  "The Build starts by itself (build, IPFS upload, start test). When it passes, the update is merged and released automatically on the first bot run 20 hours after this PR's last commit, unless it is a major upgrade (then merge by hand) or you add the `hold` label. Rules: https://github.com/AvadoDServer/ci-release-action/tree/main/automerge",
  "",
  "_Opened by the daily upstream check (.github/workflows/upstream-check.yml). If a newer version comes out first, the same PR is updated._",
].join("\n");

if (SUMMARY) writeFileSync(SUMMARY, md + "\n");
else console.log(`\n${md}`);
if (process.env.GITHUB_OUTPUT) {
  appendFileSync(process.env.GITHUB_OUTPUT, `changed=true\ntitle=${title}\n`);
}
console.log(`Written: ${[...files.keys()].join(", ")}. ${title}; package ${pkg.from} -> ${pkg.to}.`);
