// lexorank: order of siblings in the tree, string of chars 0-9a-z, sorted by
// plain string compare. mirrors rank_between() in backend/lambda_function.py.

const RANK_CHARS = '0123456789abcdefghijklmnopqrstuvwxyz'

// string strictly between the two ranks; either side can be null (open end).
// generated ranks never end with '0', so a midpoint always exists.
export function rankBetween(rankPrev, rankNext) {
  const prev = rankPrev || ''
  const next = rankNext || ''
  const result = []
  let i = 0
  while (true) {
    const digitPrev = i < prev.length ? RANK_CHARS.indexOf(prev[i]) : 0
    const digitNext = i < next.length ? RANK_CHARS.indexOf(next[i]) : RANK_CHARS.length
    if (digitNext - digitPrev > 1) {
      result.push(RANK_CHARS[Math.floor((digitPrev + digitNext) / 2)])
      return result.join('')
    }
    result.push(RANK_CHARS[digitPrev])
    i += 1
  }
}
