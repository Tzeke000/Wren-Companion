/**
 * Phase 60 — Ava mineflayer bot.
 *
 * Communicates with Python wrapper via stdin/stdout JSON protocol.
 * Each line on stdout is a JSON response to a command.
 * Each line on stdin is a JSON command.
 *
 * Bootstrap: Ava develops her own playstyle. Builder/fighter/explorer
 * nature emerges from what she actually does repeatedly.
 */
const mineflayer = require("mineflayer");
const readline = require("readline");

let bot = null;

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function handleCommand(cmd) {
  const { id, action, params } = cmd;

  if (action === "connect") {
    if (bot) {
      try { bot.quit(); } catch (e) {}
    }
    const { host = "localhost", port = 25565, username = "Ava" } = params || {};
    try {
      bot = mineflayer.createBot({ host, port, username: String(username) });
      bot.once("spawn", () => send({ id, ok: true, event: "spawned", username }));
      bot.on("chat", (u, msg) => send({ event: "chat", username: u, message: msg }));
      bot.on("death", () => send({ event: "death" }));
      bot.on("error", (e) => send({ event: "error", error: String(e) }));
      bot.on("end", () => send({ event: "disconnected" }));
    } catch (e) {
      send({ id, ok: false, error: String(e) });
    }
    return;
  }

  if (!bot) { send({ id, ok: false, error: "not connected" }); return; }

  if (action === "get_state") {
    try {
      const pos = bot.entity?.position;
      send({
        id, ok: true,
        position: pos ? { x: Math.round(pos.x), y: Math.round(pos.y), z: Math.round(pos.z) } : null,
        health: bot.health,
        food: bot.food,
        players: Object.keys(bot.players || {}).slice(0, 20),
        biome: bot.blockAt(pos)?.biome?.name || "unknown",
      });
    } catch (e) { send({ id, ok: false, error: String(e) }); }

  } else if (action === "chat") {
    try { bot.chat(String((params || {}).message || "")); send({ id, ok: true }); }
    catch (e) { send({ id, ok: false, error: String(e) }); }

  } else if (action === "move_to") {
    try {
      const { x, y, z } = params || {};
      const { pathfinder, goals: { GoalBlock } } = require("mineflayer-pathfinder");
      bot.loadPlugin(pathfinder);
      bot.pathfinder.setGoal(new GoalBlock(Number(x), Number(y), Number(z)));
      send({ id, ok: true, moving_to: { x, y, z } });
    } catch (e) { send({ id, ok: false, error: String(e) }); }

  } else if (action === "look_at") {
    try {
      const { x, y, z } = params || {};
      bot.lookAt({ x: Number(x), y: Number(y), z: Number(z) });
      send({ id, ok: true });
    } catch (e) { send({ id, ok: false, error: String(e) }); }

  } else if (action === "attack_entity") {
    try {
      const eid = params?.entity_id;
      const entity = bot.entities[eid];
      if (!entity) { send({ id, ok: false, error: "entity not found" }); return; }
      bot.attack(entity);
      send({ id, ok: true });
    } catch (e) { send({ id, ok: false, error: String(e) }); }

  } else if (action === "place_block") {
    try {
      const { x, y, z } = params || {};
      const refBlock = bot.blockAt({ x: Number(x), y: Number(y) - 1, z: Number(z) });
      if (!refBlock) { send({ id, ok: false, error: "reference block not found" }); return; }
      bot.placeBlock(refBlock, { x: 0, y: 1, z: 0 });
      send({ id, ok: true });
    } catch (e) { send({ id, ok: false, error: String(e) }); }

  } else if (action === "break_block") {
    try {
      const { x, y, z } = params || {};
      const block = bot.blockAt({ x: Number(x), y: Number(y), z: Number(z) });
      if (!block) { send({ id, ok: false, error: "block not found" }); return; }
      bot.dig(block, (err) => {
        if (err) send({ id, ok: false, error: String(err) });
        else send({ id, ok: true });
      });
    } catch (e) { send({ id, ok: false, error: String(e) }); }

  } else if (action === "get_nearby_players") {
    try {
      const players = Object.values(bot.players || {}).map(p => ({
        username: p.username,
        gamemode: p.gamemode,
      }));
      send({ id, ok: true, players });
    } catch (e) { send({ id, ok: false, error: String(e) }); }

  } else if (action === "disconnect") {
    try { bot.quit(); bot = null; send({ id, ok: true }); }
    catch (e) { send({ id, ok: false, error: String(e) }); }

  } else {
    send({ id, ok: false, error: `unknown action: ${action}` });
  }
}

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  line = line.trim();
  if (!line) return;
  try { handleCommand(JSON.parse(line)); }
  catch (e) { send({ ok: false, error: `parse error: ${e}` }); }
});

send({ event: "ready" });
