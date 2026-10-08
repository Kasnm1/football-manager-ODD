import { cp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { build } from "esbuild";
import { minify } from "html-minifier-terser";


const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function argument(name, fallback) {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const sourceRoot = path.resolve(root, argument("--source", "web"));
const outputRoot = path.resolve(root, argument("--output", "build/web_release"));
const version = argument("--version", "").replace(/^V/i, "");
if (!version) {
  throw new Error("--version is required");
}

await rm(outputRoot, { recursive: true, force: true });
await mkdir(outputRoot, { recursive: true });
await cp(sourceRoot, outputRoot, {
  recursive: true,
  filter(source) {
    const relative = path.relative(sourceRoot, source).replaceAll("\\", "/");
    return !["app.js", "app.css", "index.html"].includes(relative)
      && !relative.endsWith(".map");
  },
});

async function stripSourceMapComments(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const target = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      await stripSourceMapComments(target);
    } else if (/\.(?:css|js)$/i.test(entry.name)) {
      const content = await readFile(target, "utf8");
      await writeFile(
        target,
        content.replace(/^\s*\/\/[#@]\s*sourceMappingURL=.*$/gm, "")
          .replace(/^\s*\/\*[#@]\s*sourceMappingURL=.*?\*\/\s*$/gm, ""),
        "utf8",
      );
    }
  }
}
await stripSourceMapComments(outputRoot);

await build({
  entryPoints: [path.join(sourceRoot, "app.js")],
  outfile: path.join(outputRoot, "app.js"),
  bundle: true,
  minify: true,
  sourcemap: false,
  legalComments: "none",
  target: ["chrome109"],
  plugins: [{
    name: "fmodd-release-version",
    setup(builder) {
      builder.onLoad({ filter: /app\.js$/ }, async (args) => ({
        contents: (await readFile(args.path, "utf8")).replaceAll("4.2-dev", `V${version}`),
        loader: "js",
      }));
    },
  }],
});
await build({
  entryPoints: [path.join(sourceRoot, "app.css")],
  outfile: path.join(outputRoot, "app.css"),
  bundle: true,
  minify: true,
  sourcemap: false,
  legalComments: "none",
  target: ["chrome109"],
  external: ["/assets/*"],
});

const sourceHtml = await readFile(path.join(sourceRoot, "index.html"), "utf8");
const productionHtml = sourceHtml
  .replaceAll("4.2-dev", `V${version}`)
  .replace(/<title>[^<]*<\/title>/i, `<title>FMODD V${version}</title>`);
const minifiedHtml = await minify(productionHtml, {
  collapseWhitespace: true,
  collapseBooleanAttributes: true,
  decodeEntities: false,
  keepClosingSlash: true,
  removeComments: true,
  removeRedundantAttributes: true,
  sortAttributes: false,
  sortClassName: false,
  useShortDoctype: true,
});
await writeFile(path.join(outputRoot, "index.html"), `${minifiedHtml}\n`, "utf8");

console.log(`production web assets: ${path.relative(root, outputRoot)} (V${version}, no source maps)`);
