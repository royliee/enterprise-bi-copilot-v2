"use client";

import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import {
  ArrowUp,
  Bot,
  ChevronDown,
  Database,
  LoaderCircle,
  Sparkles,
  UserRound,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export type SqlRecord = Record<string, string | number | null>;
export type PolicyMatch = { text: string; source: string; score: number };
export type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  sqlQuery?: string;
  records?: SqlRecord[];
  policyMatches?: PolicyMatch[];
};

type ChatProps = {
  messages: ChatMessage[];
  disabled: boolean;
  isLoading: boolean;
  error?: string;
  onSubmit: (message: string) => void;
};

const promptSuggestions = [
  "Summarize Q3 revenue by region",
  "Top 5 sales performers",
  "Policy check for expense claims",
  "Compare this quarter with last quarter",
];

export default function Chat({
  messages,
  disabled,
  isLoading,
  error,
  onSubmit,
}: ChatProps) {
  const [input, setInput] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isLoading]);

  function submitMessage(message: string) {
    const trimmedMessage = message.trim();
    if (!trimmedMessage || disabled) return;
    setInput("");
    onSubmit(trimmedMessage);
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    submitMessage(input);
  }

  function handleInputKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submitMessage(input);
    }
  }

  return (
    <section className="bi-chat" aria-label="Conversation">
      <div className="bi-message-stream" aria-live="polite">
        {messages.length === 0 ? (
          <div className="bi-empty-state">
            <div className="bi-empty-icon">
              <Sparkles size={20} aria-hidden="true" />
            </div>
            <p className="bi-eyebrow">Your business, in context</p>
            <h1>What would you like to understand?</h1>
            <p className="bi-empty-description">
              Ask a question about your data or company policies. Your
              assistant will bring the relevant context together.
            </p>
            <div className="bi-prompt-list" aria-label="Suggested questions">
              {promptSuggestions.map((prompt) => (
                <button
                  className="bi-prompt-chip"
                  disabled={disabled}
                  key={prompt}
                  onClick={() => submitMessage(prompt)}
                  type="button"
                >
                  {prompt}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="bi-messages">
            {messages.map((message, index) => (
              <article
                className={`bi-message bi-message-${message.role}`}
                key={`${message.role}-${index}`}
              >
                <div className="bi-message-avatar" aria-hidden="true">
                  {message.role === "user" ? (
                    <UserRound size={16} />
                  ) : (
                    <Bot size={16} />
                  )}
                </div>
                <div className="bi-message-body">
                  <p className="bi-message-author">
                    {message.role === "user" ? "You" : "Enterprise BI Assistant"}
                  </p>
                  <div className="bi-markdown">
                    {message.role === "assistant" ? (
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {message.content}
                      </ReactMarkdown>
                    ) : (
                      <p>{message.content}</p>
                    )}
                  </div>
                  {message.sqlQuery && (
                    <details className="bi-sql-details">
                      <summary>
                        <Database size={15} aria-hidden="true" />
                        <span>View Generated SQL</span>
                        <ChevronDown
                          className="bi-details-chevron"
                          size={15}
                          aria-hidden="true"
                        />
                      </summary>
                      <pre>
                        <code>{message.sqlQuery}</code>
                      </pre>
                    </details>
                  )}
                  {!!message.records?.length && (
                    <div className="bi-result-preview">
                      <div className="bi-result-heading">
                        <strong>Data preview</strong>
                        <span>
                          {message.records.length.toLocaleString()}{" "}
                          {message.records.length === 1 ? "row" : "rows"}
                        </span>
                      </div>
                      <div className="bi-table-scroll">
                        <table>
                          <thead>
                            <tr>
                              {Object.keys(message.records[0]).map((column) => (
                                <th key={column} scope="col">
                                  {column}
                                </th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {message.records.slice(0, 8).map((record, rowIndex) => (
                              <tr key={rowIndex}>
                                {Object.keys(message.records?.[0] || {}).map(
                                  (column) => (
                                    <td key={column}>
                                      {record[column] == null
                                        ? "—"
                                        : String(record[column])}
                                    </td>
                                  ),
                                )}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                      {message.records.length > 8 && (
                        <p className="bi-preview-note">
                          Showing the first 8 rows of {message.records.length}.
                        </p>
                      )}
                    </div>
                  )}
                  {!!message.policyMatches?.length && (
                    <div className="bi-policy-context">
                      <strong>Policy sources</strong>
                      {message.policyMatches.map((match, matchIndex) => (
                        <p key={`${match.source}-${matchIndex}`}>
                          <span>{match.source}</span>
                          {match.text}
                        </p>
                      ))}
                    </div>
                  )}
                </div>
              </article>
            ))}
            {isLoading && (
              <div className="bi-thinking" role="status">
                <LoaderCircle className="spin" size={16} />
                <span>Working on your question…</span>
              </div>
            )}
          </div>
        )}
        {error && (
          <div className="bi-error" role="alert">
            {error}
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      <div className="bi-composer-area">
        <form className="bi-composer" onSubmit={handleSubmit}>
          <textarea
            aria-label="Ask about your data or policies"
            disabled={disabled}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={handleInputKeyDown}
            placeholder="Ask about your data or policies..."
            rows={1}
            value={input}
          />
          <button
            aria-label={isLoading ? "Assistant is responding" : "Send message"}
            disabled={disabled || !input.trim()}
            type="submit"
          >
            {isLoading ? (
              <LoaderCircle className="spin" size={18} />
            ) : (
              <ArrowUp size={18} />
            )}
          </button>
        </form>
        <p className="bi-disclaimer">
          AI can make mistakes. Verify important business decisions with the
          underlying data and policy sources.
        </p>
      </div>
    </section>
  );
}
