const http = require("node:http");
const { readFile } = require("node:fs/promises");
const { resolve } = require("node:path");

const directory = resolve(__dirname, "../../packages/borrowbox-server/static");
const files = new Map([
  ["/", ["index.html", "text/html"]],
  ["/static/app.js", ["app.js", "text/javascript"]],
  ["/static/app.css", ["app.css", "text/css"]],
]);

http.createServer(async (request, response) => {
  const entry = files.get(new URL(request.url, "http://localhost").pathname);
  if (!entry) {
    response.writeHead(404);
    response.end();
    return;
  }
  try {
    let content = await readFile(resolve(directory, entry[0]), "utf8");
    if (entry[0] === "index.html") {
      content = content.replace('<script src="/static/app.js">', '<script>window.YOTO_API_URL=location.origin;</script><script src="/static/app.js">');
    }
    response.writeHead(200, { "Content-Type": `${entry[1]}; charset=utf-8`, "Cache-Control": "no-store" });
    response.end(content);
  } catch {
    response.writeHead(500);
    response.end();
  }
}).listen(Number(process.env.UI_TEST_PORT || 8012), "127.0.0.1");