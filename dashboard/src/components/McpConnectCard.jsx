import React, { useState, useCallback } from 'react';
import { Copy, Check } from 'lucide-react';
import { getApiUrl } from '../config';

// How to drive this install from an agent through its MCP server (/mcp).
// The dashboard proxies /api and /videos to the backend but not /mcp, so a
// client talks to the API itself: VITE_API_URL when set, else port 8000 on
// the same host. There are no accounts here, so no key travels.
function mcpUrl() {
  const u = getApiUrl('/mcp');
  if (u.startsWith('http')) return u;
  try { return `${window.location.protocol}//${window.location.hostname}:8000/mcp`; } catch { return 'http://localhost:8000/mcp'; }
}

function clients(url) {
  return [
    {
      id: 'claude-code', label: 'Claude Code',
      snippet: `claude mcp add --transport http shorts-maxxing ${url}`,
      note: 'Run once; the tools then show up in every Claude Code session.',
    },
    {
      id: 'claude-desktop', label: 'Claude Desktop',
      snippet: `{\n  "mcpServers": {\n    "shorts-maxxing": {\n      "command": "npx",\n      "args": ["-y", "mcp-remote", "${url}"]\n    }\n  }\n}`,
      note: 'Settings → Developer → Edit config, paste it in, restart Claude.',
    },
    {
      id: 'cursor', label: 'Cursor',
      snippet: `{\n  "mcpServers": {\n    "shorts-maxxing": {\n      "url": "${url}"\n    }\n  }\n}`,
      note: 'Settings → MCP → Add new global MCP server.',
    },
    {
      id: 'n8n', label: 'n8n',
      snippet: url,
      note: 'MCP Client Tool node on your AI Agent · Transport: HTTP Streamable · Authentication: none.',
    },
    {
      id: 'curl', label: 'curl',
      snippet: `curl -X POST ${url} \\\n  -H "Content-Type: application/json" \\\n  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`,
      note: 'Plain JSON-RPC over HTTP: the endpoint every client above talks to.',
    },
  ];
}

export default function McpConnectCard() {
  const list = clients(mcpUrl());
  const [active, setActive] = useState(list[0].id);
  const [copied, setCopied] = useState(false);
  const current = list.find((c) => c.id === active) || list[0];

  const copy = useCallback(() => {
    navigator.clipboard?.writeText(current.snippet).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    }).catch(() => {});
  }, [current]);

  return (
    <div>
      <div className="flex flex-wrap gap-1.5 mb-3" role="tablist" aria-label="agent client">
        {list.map((c) => (
          <button
            key={c.id}
            role="tab"
            aria-selected={c.id === active}
            onClick={() => { setActive(c.id); setCopied(false); }}
            className={`chip ${c.id === active ? 'chip-on' : ''}`}
          >
            {c.label}
          </button>
        ))}
      </div>
      <div className="relative">
        <pre className="font-mono text-ink2 whitespace-pre-wrap break-all rounded-input border border-rule bg-paper p-3 pr-20 text-xs leading-relaxed">
          {current.snippet}
        </pre>
        <button onClick={copy} className="btn-quiet absolute top-2 right-2 px-2.5 py-1 text-xs" aria-label="copy">
          {copied ? <Check size={13} /> : <Copy size={13} />} {copied ? 'copied' : 'copy'}
        </button>
      </div>
      <p className="text-muted text-xs mt-2">{current.note}</p>
    </div>
  );
}
