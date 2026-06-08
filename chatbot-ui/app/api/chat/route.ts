export async function POST(request: Request) {
  const body = await request.json()

  const res = await fetch(
    process.env.AI_SERVICE_URL ?? 'http://localhost:8080/chat',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }
  )

  return new Response(res.body, {
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
    },
  })
}
