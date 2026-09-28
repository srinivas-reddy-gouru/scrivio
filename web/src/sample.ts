/** An invented resume and an invented posting, for a first look.
 *
 * Nobody should have to hand over their own resume to find out what this
 * does with one. The person, the employer, and the posting below do not
 * exist: the address is on example.com and the phone number is in the
 * range reserved for fiction.
 *
 * This is the same invented person the demo examples are about, so that
 * in demo mode what comes back is about what went in. A test holds the
 * two together (tests/test_sample_matches_demo.py).
 */
export const SAMPLE_NAME = "Jordan Rivera";

export const SAMPLE_RESUME = `Jordan Rivera
Backend Engineer
jordan@example.com | +1 555 010 1234 | Austin, TX

Summary
Backend engineer focused on event-driven systems.

Experience
Software Engineer, Acme Corp
Jan 2021 - Present
- Built Kafka pipelines processing 2M events/day
- Cut p99 latency 40% by rewriting the consumer group logic
- Mentored two junior engineers

Education
B.S. in Computer Science, State University, 2015 - 2019

Skills
Languages: Python, Go
Infrastructure: Kafka, PostgreSQL

Certifications
AWS Solutions Architect Associate
`;

export const SAMPLE_JD = `Senior Backend Engineer, Payments Platform

We are looking for a backend engineer to own our event-driven payment services. You will build and operate Kafka pipelines, run them on Kubernetes, and work across teams with the people who depend on them.

Requirements:
- Python or Go
- Kafka pipeline experience
- Kubernetes in production
- PostgreSQL
- Cross-team collaboration
`;

/* Home asks for the sample; the resume page takes the request when it
 * opens. Held in memory and taken once, so that coming back to the page
 * later does not overwrite what has been typed since. */
let requested = false;
export const requestSample = (): void => { requested = true; };
export const takeSampleRequest = (): boolean => {
  const was = requested;
  requested = false;
  return was;
};
