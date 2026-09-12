import { Link } from "react-router-dom";
import { Search, ArrowLeft } from "lucide-react";

/**
 * The public Privacy Policy / Terms of Service pages (Phase E6 — Google
 * OAuth verification requires both to be reachable WITHOUT logging in, at
 * stable URLs). One component, two documents: the content differs but the
 * layout is shared.
 *
 * Every claim here describes what the platform ACTUALLY does — Gmail tokens
 * are encrypted at rest, reply detection reads From/Subject headers only,
 * disconnect deletes tokens, and no data is ever sold. If the backend's
 * behavior changes, this text must change with it.
 */

const LAST_UPDATED = "September 12, 2026";
const CONTACT_EMAIL = "support@leadhuntarpro.online";

type Section = { heading: string; paras: string[]; bullets?: string[] };

const PRIVACY: Section[] = [
  {
    heading: "1. Who we are",
    paras: [
      `LeadHunter Pro ("we", "the service") is a B2B lead-research and email-outreach platform operated at leadhuntarpro.online. This policy explains what data the service collects, why, and how you stay in control of it. Questions: ${CONTACT_EMAIL}.`,
    ],
  },
  {
    heading: "2. What we collect",
    paras: ["The service runs on your account data plus the leads you research:"],
    bullets: [
      "Account data — your username, email address, and a salted hash of your password (the password itself is never stored or recoverable).",
      "Gmail connection data — when you connect a Gmail account, Google sends us OAuth access and refresh tokens, which we store encrypted at rest. We never see or store your Google password.",
      "Lead research data — company and contact details (names, roles, public pages) that our pipeline researches from publicly available web sources for the searches you run.",
      "Outreach data — the campaigns you write, which leads were emailed, when, and from which connected account.",
      "Reply metadata — to detect who answered you, we read only the From and Subject headers of your inbox. Email bodies are never read, and nothing in your inbox is ever modified or deleted.",
      "Activity logs — sign-ins and searches, for security and administration.",
    ],
  },
  {
    heading: "3. How we use your data",
    paras: [
      "Your data is used only to operate the service for you: researching the leads you ask for, sending the campaigns you write through the Gmail account you connected, detecting replies, and keeping the platform secure. We do not sell, rent, or share your data with advertisers or data brokers. We do not use your Gmail content to train anything.",
    ],
  },
  {
    heading: "4. Gmail API use and restricted scopes",
    paras: [
      "The service requests two Gmail scopes, each for one purpose:",
    ],
    bullets: [
      "gmail.send — to send the outreach emails you compose, from your connected account, at the pace you configure.",
      "gmail.readonly — to read inbox From/Subject headers so a reply can stop your follow-up sequence. It reads nothing else.",
      "openid, email, profile — to know which Gmail account was connected. No extra permissions.",
    ],
  },
  {
    heading: "5. Deleting your data",
    paras: [
      "You can remove data yourself at any time:",
    ],
    bullets: [
      "Disconnect a Gmail account (Settings → Email accounts) — the stored OAuth tokens are deleted immediately and the service loses all access to that mailbox.",
      "Delete leads and research results from the Companies screen.",
      "For full account deletion, contact us at " + CONTACT_EMAIL + " and we will remove your account and its data.",
    ],
  },
  {
    heading: "6. Security",
    paras: [
      "All traffic runs over HTTPS. Passwords are hashed, never stored in readable form. Gmail OAuth tokens are encrypted at rest with a key that never leaves the server. Access to your data requires your authenticated session.",
    ],
  },
  {
    heading: "7. Third parties",
    paras: [
      "The only third party involved in your email connection is Google (OAuth sign-in and the Gmail API). Lead research uses public web sources. We do not hand your account or Gmail data to anyone else.",
    ],
  },
  {
    heading: "8. Changes",
    paras: [
      `If this policy changes, the new version appears on this page with a new "last updated" date. Material changes are announced in the service. Last updated: ${LAST_UPDATED}.`,
    ],
  },
];

const TERMS: Section[] = [
  {
    heading: "1. The service",
    paras: [
      "LeadHunter Pro provides B2B lead research and email outreach tooling. You provide the search criteria and the email content; the service researches public information and sends emails through the Gmail account you connect.",
    ],
  },
  {
    heading: "2. Your account",
    paras: [
      "You are responsible for your account credentials and for keeping them confidential. You must provide accurate information when creating the account. We may suspend accounts that abuse the service.",
    ],
  },
  {
    heading: "3. Acceptable use",
    paras: [
      "You are responsible for the emails you send and for complying with the laws that apply to your outreach — including anti-spam laws (such as CAN-SPAM and GDPR where relevant), which generally require honest sender identity, a way to opt out, and that you only contact business addresses for legitimate commercial purposes. The service enforces conservative sending limits per Gmail account, but the content and targeting of your outreach are yours.",
    ],
  },
  {
    heading: "4. Gmail connection",
    paras: [
      "Connecting a Gmail account is optional and governed by Google's terms as well as this one. Disconnecting the account at any time revokes the service's access.",
    ],
  },
  {
    heading: "5. No warranty",
    paras: [
      "The service is provided \"as is\" without warranties of any kind. Research results come from public sources and may be incomplete or out of date; email deliverability depends on providers we do not control (including Google's spam filtering and sending limits).",
    ],
  },
  {
    heading: "6. Limitation of liability",
    paras: [
      "To the maximum extent permitted by law, the service's operators are not liable for indirect or consequential damages arising from your use of the service, including lost profits, lost data, or email deliverability problems.",
    ],
  },
  {
    heading: "7. Termination and changes",
    paras: [
      `You may stop using the service and request account deletion at any time (${CONTACT_EMAIL}). We may change or discontinue features; material changes will be announced. Last updated: ${LAST_UPDATED}.`,
    ],
  },
];

function LegalDoc({
  title,
  subtitle,
  sections,
}: {
  title: string;
  subtitle: string;
  sections: Section[];
}) {
  return (
    <div className="min-h-screen bg-[#0B0E14] text-slate-300">
      <div className="mx-auto max-w-3xl px-6 py-12">
        <Link
          to="/"
          className="inline-flex items-center gap-1.5 text-[12.5px] text-slate-500 hover:text-slate-300"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          Back to LeadHunter Pro
        </Link>

        <div className="mt-6 flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center">
            <Search className="w-4.5 h-4.5 text-white" strokeWidth={2.5} />
          </div>
          <span className="text-[15px] font-semibold text-white">
            LeadHunter <span className="text-indigo-400">Pro</span>
          </span>
        </div>

        <h1 className="mt-8 text-2xl font-semibold text-white">{title}</h1>
        <p className="mt-1 text-[13px] text-slate-500">
          {subtitle} · Last updated {LAST_UPDATED}
        </p>

        <div className="mt-8 space-y-8 pb-16">
          {sections.map((s) => (
            <section key={s.heading}>
              <h2 className="text-[15px] font-semibold text-white">{s.heading}</h2>
              {s.paras.map((p, i) => (
                <p key={i} className="mt-2 text-[13.5px] leading-relaxed text-slate-400">
                  {p}
                </p>
              ))}
              {s.bullets && (
                <ul className="mt-2 space-y-1.5">
                  {s.bullets.map((b, i) => (
                    <li
                      key={i}
                      className="flex gap-2.5 text-[13.5px] leading-relaxed text-slate-400"
                    >
                      <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-slate-600" />
                      {b}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          ))}
          <p className="border-t border-white/5 pt-6 text-[12.5px] text-slate-500">
            Questions about this document? Contact{" "}
            <a
              href={`mailto:${CONTACT_EMAIL}`}
              className="text-indigo-400 hover:text-indigo-300"
            >
              {CONTACT_EMAIL}
            </a>
            .{" "}
            <Link to="/privacy" className="text-indigo-400 hover:text-indigo-300">
              Privacy Policy
            </Link>{" "}
            ·{" "}
            <Link to="/terms" className="text-indigo-400 hover:text-indigo-300">
              Terms of Service
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}

export function PrivacyPolicy() {
  return (
    <LegalDoc
      title="Privacy Policy"
      subtitle="What LeadHunter Pro collects, why, and how you delete it"
      sections={PRIVACY}
    />
  );
}

export function TermsOfService() {
  return (
    <LegalDoc
      title="Terms of Service"
      subtitle="The rules for using LeadHunter Pro"
      sections={TERMS}
    />
  );
}
