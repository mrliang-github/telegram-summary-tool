import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { createInterface } from "node:readline/promises";

import { TelegramClient } from "telegram";
import { StringSession } from "telegram/sessions/index.js";

function parseArg(flag, fallback = undefined) {
  const idx = process.argv.indexOf(flag);
  if (idx === -1) {
    return fallback;
  }
  return process.argv[idx + 1] ?? fallback;
}

function hasFlag(flag) {
  return process.argv.includes(flag);
}

function renderHelp() {
  console.log(`
GramJS chat fetcher

Usage:
  node fetch-chat.mjs --chat <chat_username_or_id_or_title> [options]

Options:
  --chat <value>         Required. Chat username/id/title.
  --limit <number>       Number of messages to pull (default: 500).
  --out <path>           Output JSON path (default: ./result.json).
  --session-file <path>  Session file for reuse (default: ./.gramjs.session).
  --api-id <id>          Telegram API ID (or env TG_API_ID).
  --api-hash <hash>      Telegram API hash (or env TG_API_HASH).
  --help                 Show this help.

Example:
  TG_API_ID=123456 TG_API_HASH=xxxx node fetch-chat.mjs --chat my_group --limit 800 --out ./result.json
`);
}

async function readExistingSession(sessionFile) {
  try {
    const content = await fs.readFile(sessionFile, "utf-8");
    return content.trim();
  } catch {
    return "";
  }
}

function toIsoNoTimezone(date) {
  return date.toISOString().replace(".000Z", "");
}

function displayNameFromSender(sender) {
  if (!sender) {
    return "Unknown";
  }
  if (sender.title) {
    return String(sender.title);
  }
  const first = sender.firstName ? String(sender.firstName) : "";
  const last = sender.lastName ? String(sender.lastName) : "";
  const full = [first, last].filter(Boolean).join(" ").trim();
  if (full) {
    return full;
  }
  if (sender.username) {
    return String(sender.username);
  }
  if (sender.id) {
    return String(sender.id);
  }
  return "Unknown";
}

async function main() {
  if (hasFlag("--help")) {
    renderHelp();
    return;
  }

  const chat = parseArg("--chat");
  if (!chat) {
    renderHelp();
    process.exitCode = 1;
    return;
  }

  const limit = Number(parseArg("--limit", "500"));
  const outputPath = path.resolve(parseArg("--out", "result.json"));
  const sessionFile = path.resolve(parseArg("--session-file", ".gramjs.session"));
  const apiId = Number(parseArg("--api-id", process.env.TG_API_ID));
  const apiHash = parseArg("--api-hash", process.env.TG_API_HASH);

  if (!apiId || !apiHash) {
    console.error("Missing Telegram API credentials. Set --api-id/--api-hash or TG_API_ID/TG_API_HASH.");
    process.exitCode = 1;
    return;
  }

  const rl = createInterface({
    input: process.stdin,
    output: process.stdout,
  });

  const savedSession = await readExistingSession(sessionFile);
  const client = new TelegramClient(new StringSession(savedSession), apiId, apiHash, {
    connectionRetries: 5,
  });

  try {
    await client.start({
      phoneNumber: async () => rl.question("Phone number: "),
      password: async () => rl.question("2FA password (if enabled): "),
      phoneCode: async () => rl.question("Login code: "),
      onError: (err) => console.error("Auth error:", err),
    });

    const newSession = client.session.save();
    await fs.writeFile(sessionFile, newSession, "utf-8");

    const entity = await client.getEntity(chat);
    const chatName =
      entity.title ||
      [entity.firstName, entity.lastName].filter(Boolean).join(" ").trim() ||
      String(chat);

    const rawMessages = [];
    for await (const msg of client.iterMessages(entity, { limit })) {
      rawMessages.push(msg);
    }
    rawMessages.reverse();

    const messages = [];
    for (const msg of rawMessages) {
      const text = typeof msg.message === "string" ? msg.message.trim() : "";
      if (!text) {
        continue;
      }

      const sender = await msg.getSender();
      const from = displayNameFromSender(sender);
      const fromId =
        sender?.id != null
          ? String(sender.id)
          : msg.senderId != null
            ? String(msg.senderId)
            : "unknown";
      const replyToId = msg.replyTo?.replyToMsgId;

      messages.push({
        id: Number(msg.id),
        type: "message",
        date: toIsoNoTimezone(msg.date),
        from,
        from_id: fromId,
        text,
        ...(replyToId ? { reply_to_message_id: Number(replyToId) } : {}),
      });
    }

    const output = {
      name: chatName,
      type: "saved_from_gramjs",
      messages,
    };

    await fs.writeFile(outputPath, `${JSON.stringify(output, null, 2)}\n`, "utf-8");

    console.log(`Fetched ${messages.length} text messages from "${chatName}"`);
    console.log(`Saved export JSON to: ${outputPath}`);
    console.log(`Session saved to: ${sessionFile}`);
  } finally {
    rl.close();
    await client.disconnect();
  }
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});

