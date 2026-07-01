import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { isSafeHttpUrl } from "../lib/url";
import { cn } from "../lib/cn";

// Markdown renderer for ASSISTANT (ANTON) chat replies only.
//
// SECURITY: rehype-raw is deliberately NOT enabled and we never use
// dangerouslySetInnerHTML; `skipHtml` drops any raw HTML embedded in the markdown
// source outright (without it react-markdown renders raw HTML as escaped text —
// safe, but messy). That is the XSS-safe path, so no DOMPurify is needed. Links
// are scheme-allow-listed via isSafeHttpUrl (http/https only;
// javascript:/data:/vbscript:/blob:/file: are rejected) and images are dropped
// entirely (an unvalidated <img src> is an SSRF / tracking-pixel sink).
//
// Typography mirrors the chat bubble: base text-[14px] leading-[1.64], theme
// tokens (t1/t2/t3, accent, paper2, line) so it sits inside the redesign theme.

const COMPONENTS: Components = {
  p: ({ children }) => <p className="mb-[10px] last:mb-0">{children}</p>,
  ul: ({ children }) => (
    <ul className="list-disc pl-[20px] mb-[10px] last:mb-0 space-y-[3px]">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="list-decimal pl-[20px] mb-[10px] last:mb-0 space-y-[3px]">{children}</ol>
  ),
  li: ({ children }) => <li>{children}</li>,
  strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  code: ({ children, className }) => {
    // Fenced code blocks are wrapped in <pre> (styled below); only INLINE code
    // gets the pill treatment. react-markdown v10 dropped the `inline` prop, so
    // detect a block by its language-* className or a newline in the content —
    // otherwise the pill's background/padding double up inside the <pre>.
    const isBlock = /language-/.test(className ?? "") || String(children ?? "").includes("\n");
    return (
      <code
        className={cn(
          "mono",
          !isBlock && "text-[12.5px] bg-paper2 rounded-[4px] px-[4px] py-[1px]",
          className,
        )}
      >
        {children}
      </code>
    );
  },
  pre: ({ children }) => (
    <pre className="mono text-[12.5px] bg-paper2 rounded-[8px] p-[12px] my-[10px] overflow-x-auto">
      {children}
    </pre>
  ),
  h1: ({ children }) => (
    <h1 className="font-bold text-[18px] leading-[1.35] mt-[14px] mb-[8px] first:mt-0">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="font-bold text-[16px] leading-[1.35] mt-[14px] mb-[8px] first:mt-0">{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 className="font-bold text-[14.5px] leading-[1.35] mt-[12px] mb-[6px] first:mt-0">{children}</h3>
  ),
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 border-line pl-[12px] text-t2 my-[10px]">{children}</blockquote>
  ),
  a: ({ href, children }) =>
    isSafeHttpUrl(href) ? (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="text-accent underline hover:opacity-80"
      >
        {children}
      </a>
    ) : (
      <span>{children}</span>
    ),
  // Drop images entirely — never render an arbitrary, unvalidated <img src>.
  img: () => null,
};

export function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={COMPONENTS}>
      {children}
    </ReactMarkdown>
  );
}

export default Markdown;
