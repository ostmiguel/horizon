/**
 * Cloudflare Worker — реле уведомлений в Telegram.
 *
 * Зачем: прод-VPS (российский хостинг) не достаёт api.telegram.org напрямую.
 * Cloudflare — глобальная сеть, её RU достаёт, а она достаёт Telegram. Сервер шлёт
 * POST сюда, воркер форвардит в Telegram sendMessage.
 *
 * Настройка в Cloudflare (Workers & Pages → Create Worker → вставить этот код):
 *   Settings → Variables and Secrets (лучше как Secret):
 *     TELEGRAM_BOT_TOKEN  — токен бота
 *     TELEGRAM_CHAT_ID    — id чата (например 243607622)
 *     RELAY_SECRET        — любая длинная случайная строка (общий секрет с сервером)
 *
 * В .env сервера добавить:
 *   TELEGRAM_RELAY_URL=https://<имя-воркера>.<субдомен>.workers.dev
 *   TELEGRAM_RELAY_SECRET=<та же строка, что RELAY_SECRET>
 * и `systemctl restart horizon`.
 *
 * Проверка: curl -s -X POST "$TELEGRAM_RELAY_URL" -H 'content-type: application/json' \
 *   -d '{"secret":"<RELAY_SECRET>","text":"тест из relay"}'
 */
export default {
  async fetch(request, env) {
    if (request.method !== 'POST') {
      return new Response('Horizon telegram relay: POST only', { status: 200 });
    }
    let body;
    try {
      body = await request.json();
    } catch {
      return new Response('bad json', { status: 400 });
    }
    // Общий секрет — чтобы по URL воркера нельзя было спамить твой чат.
    if (!env.RELAY_SECRET || body.secret !== env.RELAY_SECRET) {
      return new Response('forbidden', { status: 403 });
    }
    const text = String(body.text || '').slice(0, 4000);
    if (!text) return new Response('empty text', { status: 400 });
    if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {
      return new Response('relay misconfigured (no token/chat)', { status: 500 });
    }
    const tg = await fetch(
      `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
      }
    );
    // Пробрасываем ответ Telegram как есть — удобно для отладки с сервера.
    return new Response(await tg.text(), {
      status: tg.status,
      headers: { 'content-type': 'application/json' },
    });
  },
};
