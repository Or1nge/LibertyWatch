function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeUrl(value) {
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : null;
  } catch {
    return null;
  }
}

function inlineMarkdown(value) {
  const source = String(value ?? "");
  let output = "";
  let cursor = 0;
  const links = /\[([^\]\n]{1,300})\]\(([^)\s]{1,2000})\)/g;
  for (const match of source.matchAll(links)) {
    output += escapeHtml(source.slice(cursor, match.index));
    const url = safeUrl(match[2]);
    output += url
      ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer nofollow">${escapeHtml(match[1])}</a>`
      : escapeHtml(match[0]);
    cursor = match.index + match[0].length;
  }
  return output + escapeHtml(source.slice(cursor));
}

export function renderSafeMarkdown(markdown) {
  const lines = String(markdown ?? "").replaceAll("\r\n", "\n").split("\n");
  const output = [];
  let listOpen = false;
  const closeList = () => {
    if (listOpen) output.push("</ul>");
    listOpen = false;
  };
  for (const line of lines) {
    const heading = /^(#{1,4})\s+(.+)$/.exec(line);
    const bullet = /^[-*]\s+(.+)$/.exec(line);
    if (heading) {
      closeList();
      const level = Math.min(4, heading[1].length + 2);
      output.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
    } else if (bullet) {
      if (!listOpen) output.push("<ul>");
      listOpen = true;
      output.push(`<li>${inlineMarkdown(bullet[1])}</li>`);
    } else if (!line.trim()) {
      closeList();
    } else {
      closeList();
      output.push(`<p>${inlineMarkdown(line)}</p>`);
    }
  }
  closeList();
  return output.join("\n");
}
