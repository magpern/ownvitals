// One requestAnimationFrame loop shared by every count-up (motion.md rule 3: at most one JS
// animation loop at a time). A job gets the time since it started and says whether it is done.

type Job = (elapsedMs: number) => boolean

const jobs = new Map<Job, number>()
let frame: number | null = null

function tick(now: number): void {
  frame = null
  for (const [job, startedAt] of [...jobs]) {
    if (job(Math.max(0, now - startedAt))) jobs.delete(job)
  }
  if (jobs.size > 0) frame = requestAnimationFrame(tick)
}

/** Runs `job` every frame until it returns true. Returns a function that cancels it. */
export function startJob(job: Job): () => void {
  jobs.set(job, performance.now())
  if (frame === null) frame = requestAnimationFrame(tick)
  return () => {
    jobs.delete(job)
    if (jobs.size === 0 && frame !== null) {
      cancelAnimationFrame(frame)
      frame = null
    }
  }
}
