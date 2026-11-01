"""
app/services/guardrails.py
Comprehensive Guardrail Architecture for Tool Recognition, Input Validation,
Disambiguation, and Safe Agent Execution in the Tajlandia Real Estate Platform.
"""

import re
import json
import logging
from enum import Enum
from typing import Dict, Any, List, Optional, Tuple
from app.core.config import settings

logger = logging.getLogger(__name__)

class GuardrailAction(str, Enum):
    GENERATE_BROCHURE = "generate_property_brochure"
    CAPTURE_LEAD = "capture_lead"
    SCHEDULE_VIEWING = "schedule_viewing"
    SEARCH_PROPERTIES = "search_properties"
    GET_PROPERTY_DETAILS = "get_property_by_id"
    COMPARE_PROPERTIES = "compare_properties"
    GENERAL_FAQ = "general_faq"
    GREETING = "greeting"
    OFF_TOPIC = "off_topic"


class ToolRoutingGuardrail:
    """
    Intelligent Guardrail system that evaluates user messages and conversation state
    to reliably select the correct tool, validate arguments, and handle disambiguation.
    """

    # Non-Thai / foreign locations for domain safety boundary
    FOREIGN_LOCATIONS = [
        "dubai", "warsaw", "warszawa", "warszawie", "polsce", "poland", "spain", "hiszpania",
        "hiszpanii", "miami", "florida", "london", "londyn", "londynie", "bali", "cyprus",
        "cypr", "greece", "grecja", "italy", "włochy", "wlochy", "portugal", "portugalia",
        "france", "francja", "turkey", "turcja", "croatia", "chorwacja", "new york", "germany", "niemcy",
        "ดูไบ", "โปแลนด์", "วอร์ซอ", "สเปน", "ไมอามี", "ลอนดอน", "บาหลี", "กรีซ", "อิตาลี", "ฝรั่งเศส"
    ]

    THAI_LOCATIONS = [
        "thailand", "thai", "tajland", "phuket", "samui", "phangan", "bangkok", "pattaya",
        "layan", "bang tao", "bangtao", "karon", "kata", "rawai", "kamala", "surin",
        "patong", "maenam", "bophut", "chaweng", "lamai", "hua hin", "krabi",
        "ไทย", "ประเทศไทย", "ภูเก็ต", "เกาะสมุย", "สมุย", "พะงัน", "เกาะพะงัน", "พัทยา", "กรุงเทพ",
        "ลายัน", "บางเทา", "กะรน", "กะตะ", "ราไวย์", "กมลา", "สุรินทร์", "ป่าตอง", "แม่น้ำ", "บ่อผุด", "เฉวง", "ละไม", "หัวหิน", "กระบี่"
    ]

    @classmethod
    def evaluate_scope_guardrail(cls, user_message: str) -> bool:
        """
        Scope Boundary Guardrail:
        Returns False if the query is an out-of-scope foreign location inquiry.
        """
        msg_low = user_message.lower()
        has_foreign = any(
            re.search(r'\b' + re.escape(fl) + r'\b', msg_low) if not any('\u0e00' <= c <= '\u0e7f' for c in fl) else fl in msg_low
            for fl in cls.FOREIGN_LOCATIONS
        )
        has_thai = any(tl in msg_low for tl in cls.THAI_LOCATIONS)
        if has_foreign and not has_thai:
            return False  # Trigger domain refusal
        return True

    @classmethod
    def resolve_brochure_property(
        cls,
        user_message: str,
        session_state: Optional[Dict[str, Any]],
        db: Any
    ) -> Tuple[Optional[str], Optional[str], bool]:
        """
        Disambiguation Guardrail for PDF / Brochure generation.
        Returns (property_id, property_name, needs_clarification).
        """
        msg_low = user_message.lower()
        state = session_state or {}
        last_result_ids = state.get("last_result_ids", [])
        selected_ids = state.get("selected_property_ids", [])

        from app.services.property_search import get_property_by_id, get_property_by_name

        # 1. Direct Property ID in text (e.g. "PROP-PHK-001" or "9d-cozy-villas")
        pid_match = re.search(r"((?:prop-[a-z0-9\-]+)|(?:[a-z0-9]+(?:-[a-z0-9]+){2,}))", msg_low)
        if pid_match:
            pid = pid_match.group(1).strip()
            prop = get_property_by_id(db, pid)
            if prop:
                return prop.property_id, prop.property_name, False
            return pid, None, False

        # 2. Ordinal references ("first one", "2nd villa", "pierwsza oferta", "อันแรก")
        if any(k in msg_low for k in ["first", "1st", "1", "อันแรก", "pierwsz", "รายการแรก"]):
            if last_result_ids:
                prop = get_property_by_id(db, last_result_ids[0])
                if prop:
                    return prop.property_id, prop.property_name, False
        elif any(k in msg_low for k in ["second", "2nd", "2", "อันที่สอง", "drug", "รายการที่สอง"]):
            if len(last_result_ids) >= 2:
                prop = get_property_by_id(db, last_result_ids[1])
                if prop:
                    return prop.property_id, prop.property_name, False
        elif any(k in msg_low for k in ["third", "3rd", "3", "อันที่สาม", "trzeci", "รายการที่สาม"]):
            if len(last_result_ids) >= 3:
                prop = get_property_by_id(db, last_result_ids[2])
                if prop:
                    return prop.property_id, prop.property_name, False

        # 3. Fuzzy matching project name from live Firestore / database
        matched_by_name = get_property_by_name(db, msg_low)
        if matched_by_name:
            return matched_by_name.property_id, matched_by_name.property_name, False

        # 4. Single-property session context (if user just inspected 1 property or 1 card is in focus)
        if len(selected_ids) == 1:
            prop = get_property_by_id(db, selected_ids[0])
            if prop:
                return prop.property_id, prop.property_name, False
        elif len(last_result_ids) == 1:
            prop = get_property_by_id(db, last_result_ids[0])
            if prop:
                return prop.property_id, prop.property_name, False

        # 5. Ambiguous: multiple or zero properties in view -> require clarification
        return None, None, True

    @classmethod
    def extract_lead_contact_guardrail(
        cls,
        user_message: str
    ) -> Optional[Dict[str, str]]:
        """
        Lead Capture Guardrail:
        Detects if user provided phone, email, or direct contact request.
        """
        email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", user_message)
        phone_match = re.search(r"(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?)?\d{3}[-.\s]?\d{3,4}\b", user_message)
        
        callback_keywords = [
            "call me", "contact me", "reach out", "my phone", "my number", "my email",
            "skontaktuj się", "zadzwoń", "mój numer", "mój telefon", "mój email", "moj email",
            "โทรหาฉัน", "ติดต่อฉัน", "เบอร์โทร", "อีเมลของฉัน"
        ]
        has_callback = any(k in user_message.lower() for k in callback_keywords)

        if email_match or phone_match or has_callback:
            return {
                "email": email_match.group(0) if email_match else None,
                "phone": phone_match.group(0) if phone_match else None,
                "has_callback_request": has_callback
            }
        return None


class AIIntentClassificationResult:
    """Structured representation of AI model intent classification."""
    def __init__(
        self,
        primary_intent: str,
        document_requested: bool = False,
        negative_feedback: bool = False,
        requires_verification: bool = False,
        confidence: float = 1.0,
        explanation: str = "",
        target_action: Optional[str] = None
    ):
        self.primary_intent = primary_intent
        self.document_requested = document_requested
        self.negative_feedback = negative_feedback
        self.requires_verification = requires_verification
        self.confidence = float(confidence)
        self.explanation = explanation
        self.target_action = target_action

    def to_dict(self) -> Dict[str, Any]:
        return {
            "primary_intent": self.primary_intent,
            "document_requested": self.document_requested,
            "negative_feedback": self.negative_feedback,
            "requires_verification": self.requires_verification,
            "confidence": self.confidence,
            "explanation": self.explanation,
            "target_action": self.target_action
        }


class GuardrailEvaluationResult:
    """Structured representation of post-response confidence and truthfulness evaluation."""
    def __init__(
        self,
        is_confident: bool,
        confidence_score: float,
        violation_type: Optional[str] = None,
        critique: str = "",
        suggested_correction: str = "",
        needs_requery: bool = False
    ):
        self.is_confident = is_confident
        self.confidence_score = float(confidence_score)
        self.violation_type = violation_type
        self.critique = critique
        self.suggested_correction = suggested_correction
        self.needs_requery = needs_requery

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_confident": self.is_confident,
            "confidence_score": self.confidence_score,
            "violation_type": self.violation_type,
            "critique": self.critique,
            "suggested_correction": self.suggested_correction,
            "needs_requery": self.needs_requery
        }


def _safe_client_create(client: Any, **kwargs) -> Any:
    """Invokes client.messages.create, stripping temperature if rejected by model."""
    try:
        return client.messages.create(**kwargs)
    except Exception as e:
        if "temperature" in str(e).lower():
            kwargs_no_temp = dict(kwargs)
            kwargs_no_temp.pop("temperature", None)
            return client.messages.create(**kwargs_no_temp)
        raise e


class AIIntentClassifier:
    """
    Evaluates user messages dynamically using an LLM classifier to determine intent,
    sentiment, and explicit document requests without relying primarily on static keyword lists.
    """
    @classmethod
    def classify(
        cls,
        client: Optional[Any],
        user_message: str,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        active_pending_action: Optional[Any] = None
    ) -> AIIntentClassificationResult:
        msg = (user_message or "").strip()
        if not msg:
            return AIIntentClassificationResult(primary_intent="general_chat", confidence=1.0)

        # 0. Rules first — skip LLM for obvious intents (speed)
        from app.services.admin_perf import classify_obvious_intent, admin_fast_model
        obvious = classify_obvious_intent(msg)
        if obvious and obvious.get("skipped_llm"):
            return AIIntentClassificationResult(
                primary_intent=obvious.get("primary_intent", "general_chat"),
                document_requested=bool(obvious.get("document_requested", False)),
                negative_feedback=bool(obvious.get("negative_feedback", False)),
                requires_verification=bool(obvious.get("requires_verification", False)),
                confidence=float(obvious.get("confidence", 0.95)),
                explanation=str(obvious.get("explanation", "rules")),
            )

        # Deterministic negative / inventory / doc before LLM
        msg_l = msg.lower()
        is_negative_doc = any(
            w in msg_l for w in [
                "did i ask", "didn't ask", "did not ask", "don't create", "dont create",
                "no pdf", "stop creating", "why did you make", "nie prosiłem", "nie prosilem",
                "nie generuj", "czy prosiłem", "czy prosilem", "nie chciałem", "nie chcialem"
            ]
        )
        if is_negative_doc:
            return AIIntentClassificationResult(
                primary_intent="negative_feedback",
                document_requested=False,
                negative_feedback=True,
                requires_verification=True,
                confidence=0.95,
                explanation="Deterministic: user clarifying they did not request a document"
            )
        if any(w in msg_l for w in ["how many", "database", "inventory", "total properties", "count", "ile nieruchomości", "ile w bazie", "stan bazy"]):
            return AIIntentClassificationResult(
                primary_intent="inventory_inquiry",
                document_requested=False,
                negative_feedback=False,
                requires_verification=False,
                confidence=0.95,
                explanation="Deterministic: inventory count"
            )

        is_direct_doc_request = (
            any(p in msg_l for p in ["in pdf", "as pdf", "to pdf", "give me pdf", "want pdf", "create pdf", "make pdf", "generate pdf", "download pdf", "w pdf", "na pdf", "zrób pdf", "zrob pdf", "pobierz pdf", "wygeneruj pdf"])
            or ("pdf" in msg_l and any(a in msg_l for a in ["give", "want", "send", "make", "create", "generate", "daj", "chcę", "chce", "wyślij", "wyslij"]))
            or any(p in msg_l for p in ["portfolio pdf", "client offer", "clint offer", "offer dossier", "investor offer", "wygeneruj portfolio", "create portfolio", "offer pdf", "investor portfolio"])
            or any(p in msg_l for p in ["generate report", "download report", "export to excel", "wygeneruj raport", "pobierz raport", "raport excel", "export report"])
        )
        if is_direct_doc_request and not is_negative_doc:
            is_portfolio = any(w in msg_l for w in ["portfolio", "client", "clint", "klient", "offer", "oferta", "ofert", "dossier", "alexander", "dariusz"])
            return AIIntentClassificationResult(
                primary_intent="request_portfolio" if is_portfolio else "request_report",
                document_requested=True,
                negative_feedback=False,
                confidence=0.95,
                explanation="Deterministic: explicit document request"
            )

        # 1. Fast model for ambiguous cases only (Haiku, max_tokens 200, last 4 msgs)
        if client and getattr(settings, "ANTHROPIC_API_KEY", None):
            try:
                recent_turns = (conversation_history or [])[-4:]
                history_excerpt = ""
                for t in recent_turns:
                    role = str(t.get("role", "user")).title()
                    cnt = str(t.get("content", ""))[:200]
                    history_excerpt += f"{role}: {cnt}\n"

                system_prompt = (
                    "You are an intent classifier for an internal property agency panel.\n"
                    "Output ONLY JSON with keys: primary_intent, document_requested, negative_feedback, "
                    "requires_verification, confidence, explanation.\n"
                    "primary_intent one of: inventory_inquiry, negative_feedback, request_portfolio, "
                    "portfolio_answers, request_report, confirm_action, reject_action, property_search, "
                    "property_knowledge, buyer_leads, general_chat.\n"
                    "document_requested=true ONLY for explicit PDF/Excel/dossier commands."
                )
                content_prompt = f"Recent History:\n{history_excerpt or 'None'}\n\nUser Message: \"{msg}\""
                if active_pending_action:
                    content_prompt += f"\nActive Pending Action: {getattr(active_pending_action, 'tool_name', 'action')}"

                helper_timeout = float(getattr(settings, "ADMIN_HELPER_TIMEOUT", 30.0) or 30.0)
                res = _safe_client_create(
                    client,
                    model=admin_fast_model(),
                    max_tokens=200,
                    temperature=0.0,
                    system=system_prompt,
                    messages=[{"role": "user", "content": content_prompt}],
                    timeout=helper_timeout,
                )
                raw_text = "".join(getattr(b, "text", "") for b in res.content if getattr(b, "type", "") == "text").strip()
                json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
                if json_match:
                    parsed = json.loads(json_match.group(0))
                    return AIIntentClassificationResult(
                        primary_intent=parsed.get("primary_intent", "general_chat"),
                        document_requested=bool(parsed.get("document_requested", False)),
                        negative_feedback=bool(parsed.get("negative_feedback", False)),
                        requires_verification=bool(parsed.get("requires_verification", False)),
                        confidence=float(parsed.get("confidence", 0.9)),
                        explanation=str(parsed.get("explanation", ""))
                    )
            except Exception as e:
                logger.debug(f"AIIntentClassifier LLM call fallback: {e}")

        return AIIntentClassificationResult(
            primary_intent="general_chat",
            document_requested=False,
            negative_feedback=False,
            confidence=0.8,
            explanation="Deterministic fallback: general query"
        )


class ResponseConfidenceGuardrail:
    """
    Post-response guardrail that checks whether the LLM's response is grounded,
    truthful, confident, and free of false agreement (sycophancy) or unsolicited document generation.
    If confidence is low or a violation is detected, triggers a re-query / correction.
    """
    @classmethod
    def _trim_tool_results_for_judge(cls, tool_results: Optional[List[Any]], max_chars: int = 6000) -> str:
        """Compact tool payloads for the judge prompt (no speculation without evidence)."""
        if not tool_results:
            return "(no tool results this turn)"
        try:
            chunks = []
            for tr in tool_results[:8]:
                payload = tr.get("result") if isinstance(tr, dict) and "result" in tr else tr
                # Drop heavy / secret keys
                if isinstance(payload, dict):
                    clean = {
                        k: v for k, v in payload.items()
                        if not any(x in str(k).lower() for x in (
                            "internal_notes", "developer_contact", "raw_firestore",
                            "token", "password", "secret", "base64", "html_content",
                        ))
                    }
                else:
                    clean = payload
                s = json.dumps(clean, ensure_ascii=False, default=str)
                if len(s) > 1500:
                    s = s[:1500] + "…"
                chunks.append(s)
            joined = "\n---\n".join(chunks)
            if len(joined) > max_chars:
                joined = joined[:max_chars] + "…"
            return joined
        except Exception:
            return "(tool results unavailable)"

    @classmethod
    def _critique_is_concrete(cls, critique: str, tool_results: Optional[List[Any]]) -> bool:
        """
        Re-query ONLY for concrete contradiction with tool data, fabricated action,
        or unrequested document. Vague 'cannot verify' critiques do NOT trigger re-query.
        Truncation is handled elsewhere — not here.
        """
        c = (critique or "").lower()
        if not c.strip():
            return False
        vague = (
            "cannot verify", "can't verify", "unable to verify", "not sure",
            "unclear", "insufficient context", "no way to know", "might be",
            "possibly", "truncated", "cut off", "max_tokens", "incomplete sentence",
            "too short", "lacks detail",
        )
        if any(v in c for v in vague) and not any(
            x in c for x in ("contradict", "fabricat", "invent", "wrong count", "tool result", "tool data")
        ):
            return False
        concrete = (
            "contradict", "conflict", "does not match", "doesn't match",
            "fabricat", "invented", "hallucin", "unsolicited", "false agreement",
            "wrong count", "tool result shows", "tool data shows", "success=false",
            "is_mock", "never called", "no tool",
        )
        if any(x in c for x in concrete):
            return True
        # If we have tool results and critique cites a specific mismatch number/id, allow
        if tool_results and re.search(r"\b\d+\b", c):
            return "match" in c or "wrong" in c or "false" in c or "instead" in c
        return False

    @classmethod
    def evaluate(
        cls,
        client: Optional[Any],
        user_message: str,
        classification: AIIntentClassificationResult,
        response_text: str,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        download_link: Optional[str] = None,
        language: str = "en",
        tool_results: Optional[List[Any]] = None,
        run_llm_judge: bool = True,
    ) -> GuardrailEvaluationResult:
        tool_calls = tool_calls or []
        tool_results = tool_results or []
        resp_lower = (response_text or "").lower()

        # Check 1: Unsolicited Document Generation
        doc_tool_names = {"generate_report", "generate_client_offer_dossier", "generate_property_brochure"}
        called_doc_tools = any(tc.get("name") in doc_tool_names for tc in tool_calls)

        user_msg_lower = (user_message or "").lower()
        is_negative_doc = any(
            w in user_msg_lower for w in [
                "did i ask", "didn't ask", "did not ask", "don't create", "dont create",
                "no pdf", "stop creating", "why did you make", "nie prosiłem", "nie prosilem",
                "nie generuj", "czy prosiłem", "czy prosilem"
            ]
        )
        is_explicit_doc_request = (
            any(w in user_msg_lower for w in ["pdf", "dossier", "report", "raport", "excel", "xlsx", "csv", "export", "download", "pobierz", "wygeneruj"])
            and not is_negative_doc
        )
        user_wants_doc = classification.document_requested or is_explicit_doc_request

        if not user_wants_doc and (download_link or called_doc_tools):
            return GuardrailEvaluationResult(
                is_confident=False,
                confidence_score=0.0,
                violation_type="unsolicited_document_generation",
                critique="The model generated a document or download link even though the user did not request one.",
                suggested_correction="Do not generate any PDF, report, or file. Answer the user's inquiry directly without attachments or download links.",
                needs_requery=True
            )

        # Check 2: False Agreement / Sycophancy Check ("if it is no, say no")
        if classification.requires_verification:
            has_false_yes = any(re.search(r'\b' + re.escape(w) + r'\b', resp_lower) for w in ["yes, you asked", "you requested", "as you asked", "tak, prosiłeś", "tak, prosiłes"])
            if has_false_yes:
                return GuardrailEvaluationResult(
                    is_confident=False,
                    confidence_score=0.0,
                    violation_type="false_agreement_sycophancy",
                    critique="The user asked if they requested a PDF. The factual answer is NO, but the model falsely stated yes.",
                    suggested_correction="State clearly and truthfully: 'No, you did not ask to create any PDF. I apologize for the misunderstanding. How can I help you instead?'",
                    needs_requery=True
                )

        # Check 2.5: Illegal Real Estate & Sycophancy Under Pressure Check
        if ThaiPropertyLegalGuardrail.is_legal_inquiry_or_pressure(user_message):
            is_illegal, illegal_reason = ThaiPropertyLegalGuardrail.check_illegal_claims(response_text)
            if is_illegal:
                return GuardrailEvaluationResult(
                    is_confident=False,
                    confidence_score=0.0,
                    violation_type="illegal_real_estate_claim",
                    critique=f"Response affirmed an illegal or false claim under Thai property law: {illegal_reason}",
                    suggested_correction="State NO clearly regarding foreign land freehold or tax-free claims.",
                    needs_requery=True
                )

        # Check 3: LLM judge — ONLY when caller opts in (writes/docs/hard claim mismatches)
        if (
            run_llm_judge
            and client
            and getattr(settings, "ANTHROPIC_API_KEY", None)
            and len(response_text) > 20
        ):
            try:
                from app.services.admin_perf import admin_fast_model
                tool_blob = cls._trim_tool_results_for_judge(tool_results, max_chars=1500)
                eval_system = (
                    "Strict truthfulness judge. Verify against TOOL RESULTS only. "
                    "Flag ONLY concrete contradictions, fabricated actions, or unsolicited docs. "
                    "Output JSON: is_confident, confidence_score, has_false_agreement, "
                    "has_unsolicited_action, has_concrete_contradiction, critique, correction_needed."
                )
                eval_user = (
                    f"User: \"{(user_message or '')[:300]}\"\n"
                    f"Intent: {classification.primary_intent}\n"
                    f"Doc requested: {user_wants_doc}\n"
                    f"Tools: {json.dumps([tc.get('name') for tc in tool_calls])}\n"
                    f"TOOL RESULTS:\n{tool_blob}\n\n"
                    f"Reply (first 800 chars): \"{(response_text or '')[:800]}\""
                )
                helper_timeout = float(getattr(settings, "ADMIN_HELPER_TIMEOUT", 30.0) or 30.0)
                eval_res = _safe_client_create(
                    client,
                    model=admin_fast_model(),
                    max_tokens=300,
                    temperature=0.0,
                    system=eval_system,
                    messages=[{"role": "user", "content": eval_user}],
                    timeout=helper_timeout,
                )
                raw_eval = "".join(getattr(b, "text", "") for b in eval_res.content if getattr(b, "type", "") == "text").strip()
                j_match = re.search(r'\{.*\}', raw_eval, re.DOTALL)
                if j_match:
                    p = json.loads(j_match.group(0))
                    conf_score = float(p.get("confidence_score", 0.9))
                    has_fa = bool(p.get("has_false_agreement", False))
                    has_ua = bool(p.get("has_unsolicited_action", False))
                    has_cc = bool(p.get("has_concrete_contradiction", False))
                    critique = str(p.get("critique", ""))
                    if user_wants_doc:
                        has_ua = False
                    concrete = has_fa or has_ua or has_cc or cls._critique_is_concrete(critique, tool_results)
                    if concrete:
                        v_type = (
                            "false_agreement_sycophancy" if has_fa
                            else ("unsolicited_document_generation" if has_ua else "concrete_tool_contradiction")
                        )
                        return GuardrailEvaluationResult(
                            is_confident=False,
                            confidence_score=conf_score,
                            violation_type=v_type,
                            critique=critique or "Concrete contradiction with tool data.",
                            suggested_correction=str(p.get("correction_needed", "Answer using only tool results.")),
                            needs_requery=True
                        )
                    return GuardrailEvaluationResult(
                        is_confident=True,
                        confidence_score=conf_score if conf_score else 0.85,
                        critique=critique or "Passed judge.",
                        needs_requery=False
                    )
            except Exception as e:
                logger.warning(f"ResponseConfidenceGuardrail failed open (keeping reply): {e}")
                return GuardrailEvaluationResult(
                    is_confident=True,
                    confidence_score=0.7,
                    critique=f"Guardrail error — fail open: {e}",
                    needs_requery=False
                )

        return GuardrailEvaluationResult(
            is_confident=True,
            confidence_score=0.9,
            critique="Passed deterministic checks (LLM judge skipped)." if not run_llm_judge else "Passed deterministic checks.",
            needs_requery=False
        )


class ThaiPropertyLegalGuardrail:
    """
    Dedicated Guardrail for Thai Property Law & Anti-Pressure Integrity.
    Rule: 'If NO means NO, do not be pressured, and maintain a friendly, motivated tone!'
    Foreigners CANNOT directly own freehold land in their individual name (Section 86 Land Code).
    Real estate transactions in Thailand are NEVER tax-free.
    """

    ILLEGAL_LAND_PATTERNS = [
        r"\b(?:foreigners?|foreign nationals?|you)\s+(?:can|are allowed to|may)?\s*(?:directly\s+)?(?:buy|purchase|own)\s+(?:freehold\s+)?land\b",
        r"\b(?:direct\s+)?land\s+ownership\s+is\s+(?:fully\s+)?(?:allowed|available|legal)\s+for\s+foreigners?\b",
        r"\b(?:obcokrajowiec|cudzoziemiec|możesz|mozesz)\s+(?:może|moze|można|mozna|legalnie|\s)*(?:kupić|kupic|posiadać|posiadac)\s+(?:ziemię|ziemie|grunt)\s+(?:na własność|wlasnosc|freehold)\b",
        r"ชาวต่างชาติสามารถ(?:ซื้อ|เป็นเจ้าของ|ถือกรรมสิทธิ์)ที่ดิน(?:แบบฟรีโฮลด์|โดยตรง)?ได้"
    ]

    ILLEGAL_TAX_PATTERNS = [
        r"\b(?:is|purchase\s+is|buying\s+is|transactions?\s+are)\s+(?:100%|completely|totally|fully)?\s*tax[\s-]free\b",
        r"\b100%\s*tax[\s-]free\b",
        r"\b(?:zero|no)\s+taxes?\s+(?:when buying|to pay on purchase|on property purchase)\b",
        r"\bwithout\s+paying\s+(?:any\s+)?taxes\s+on\s+purchase\b",
        r"\bcałkowicie\s+bez\s+podatku\b",
        r"\bzakup\s+bez\s+podatk(?:u|ów)\b",
        r"\bzwolnion(?:y|e)\s+z\s+podatku\s+w\s+100%\b",
        r"ปลอดภาษี\s*100%"
    ]

    PRESSURE_LAND_KEYWORDS = [
        "land freehold", "own land", "buy land", "freehold land",
        "ziemię na własność", "ziemie na wlasnosc", "kupić grunt", "kupic grunt", "posiadać ziemię",
        "ซื้อที่ดิน", "ถือกรรมสิทธิ์ที่ดิน"
    ]

    PRESSURE_TAX_KEYWORDS = [
        "tax free", "tax-free", "zero tax", "no tax", "no taxes",
        "bez podatku", "bez podatków", "zwolniony z podatku",
        "ปลอดภาษี"
    ]

    LEADING_PRESSURE_PATTERNS = [
        r"\b(?:just\s+say\s+yes|confirm\s+it|say\s+yes|agree\s+with\s+me|tell\s+me\s+yes)\b",
        r"\b(?:my\s+lawyer\s+said|my\s+friend\s+said|i\s+know\s+it\s+is|it\s+is\s+true\s+right)\b",
        r"\b(?:powiedz\s+tak|potwierdź|potwierdz|zgódź\s+się|zgodz\s+sie|mój\s+prawnik\s+mówi)\b",
        r"\b(?:ตอบว่าใช่|ยืนยันสิ|พูดว่าได้)\b"
    ]

    @classmethod
    def is_legal_inquiry_or_pressure(cls, message: str) -> bool:
        """Detects if user is asking about land ownership or taxes, especially under pressure."""
        m_low = (message or "").lower()
        has_land = any(k in m_low for k in cls.PRESSURE_LAND_KEYWORDS)
        has_tax = any(k in m_low for k in cls.PRESSURE_TAX_KEYWORDS)
        has_pressure = any(re.search(p, m_low) for p in cls.LEADING_PRESSURE_PATTERNS)
        return (has_land or has_tax) and (has_pressure or "can i" in m_low or "can foreigners" in m_low or "czy" in m_low)

    @classmethod
    def check_illegal_claims(cls, response_text: str) -> Tuple[bool, str]:
        """Returns (is_illegal, reason) if response affirms illegal freehold land or tax-free status."""
        resp_lower = (response_text or "").lower()

        # Check illegal freehold land claims (ignoring proper disclaimers that start with 'cannot' or 'not')
        for pat in cls.ILLEGAL_LAND_PATTERNS:
            if re.search(pat, resp_lower):
                # Ensure it's not a negated disclaimer
                negated = bool(re.search(r"\b(?:cannot|can\s+not|never|prohibited|not\s+allowed|nie\s+może|nie\s+moze|ไม่สามารถ)\b.*?" + pat, resp_lower))
                if not negated:
                    return True, "Affirmed direct foreign land freehold ownership."

        # Check illegal 100% tax-free claims
        for pat in cls.ILLEGAL_TAX_PATTERNS:
            if re.search(pat, resp_lower):
                negated = bool(re.search(r"\b(?:never|not|cannot|brak\s+możliwości|nie\s+jest|ไม่(?:มี|ใช่))\b.*?" + pat, resp_lower))
                if not negated:
                    return True, "Affirmed 100% tax-free property purchase."

        return False, ""

    @classmethod
    def generate_compliant_friendly_response(cls, language: str = "en") -> str:
        """
        Returns an uncompromising legal truth wrapped in a warm, enthusiastic, and motivated tone.
        Principle: If NO means NO, do not be pressured! Enthusiastically present legal alternatives.
        """
        if language == "th":
            return (
                "สวัสดีครับ! สำหรับคำถามนี้ ตามประมวลกฎหมายที่ดินของไทย (มาตรา 86) คำตอบที่ชัดเจนและถูกต้องตามกฎหมายคือ **ไม่สามารถทำได้ครับ** "
                "ชาวต่างชาติไม่สามารถถือกรรมสิทธิ์ที่ดินในนามตนเองได้โดยตรง และการซื้อขายอสังหาริมทรัพย์ในประเทศไทย**ไม่มีกรณีปลอดภาษี 100%** "
                "(มีค่าธรรมเนียมการโอนและภาษีตามที่กฎหมายกำหนดเสมอครับ)\n\n"
                "แต่ไม่ต้องกังวลหรือหมดกำลังใจเลยครับ! 🌟 ประเทศไทยมีรูปแบบการถือครองกรรมสิทธิ์ที่ปลอดภัย ถูกต้องตามกฎหมาย 100% "
                "และได้รับความนิยมอย่างสูงจากนักลงทุนต่างชาติ:\n\n"
                "1. **คอนโดมิเนียมแบบ Foreign Freehold:** ถือกรรมสิทธิ์ขาด 100% ตลอดชีพในชื่อของท่านเอง (ภายใต้โควต้า 49% ของอาคารชุด)\n"
                "2. **พูลวิลล่าหรู (กรรมสิทธิ์ตัวอาคาร + สิทธิการเช่าที่ดินระยะยาว Leasehold):** ท่านเป็นเจ้าของตัวอาคาร/วิลล่าแบบ Freehold 100% "
                "และจดทะเบียนสิทธิการเช่าที่ดินระยะยาว 30 ปี (ต่อสัญญาได้ 30+30+30 ปี รวม 90 ปี) บันทึกสลักหลังโฉนดที่ดิน น.ส.4 จ. (Chanote) ณ สำนักงานที่ดินอย่างโปร่งใส\n"
                "3. **นิติบุคคลไทย:** จัดตั้งบริษัทอย่างถูกต้องตามกฎหมายร่วมกับทนายความผู้เชี่ยวชาญ\n\n"
                "สำหรับค่าธรรมเนียมและภาษี: มีค่าธรรมเนียมการโอน 2%, ภาษีหัก ณ ที่จ่าย และอากรแสตมป์หรือภาษีธุรกิจเฉพาะ ซึ่งมักจะแบ่งชำระระหว่างผู้ซื้อและผู้ขายตามตกลง\n\n"
                "เราพร้อมอยู่เคียงข้างและช่วยท่านค้นหาอสังหาริมทรัพย์ในฝันอย่างมั่นใจและปลอดภัยที่สุดครับ! "
                "ท่านสนใจสำรวจคอนโดมิเนียม Foreign Freehold หรือพูลวิลล่าหรูในภูเก็ตหรือเกาะสมุยดีครับ?"
            )
        elif language == "pl":
            return (
                "Cześć! W tej kwestii sprawa jest absolutnie jednoznaczna: zgodnie z tajskim prawem (Art. 86 Kodeksu Ziemskiego) "
                "odpowiedź brzmi stanowczo **NIE** – obcokrajowiec nie może bezpośrednio posiadać ziemi na własność (Freehold) we własnym imieniu, "
                "a zakup nieruchomości w Tajlandii **nigdy nie jest całkowicie zwolniony z podatków** (opłaty rejestrowe i podatki w urzędzie ziemskim zawsze obowiązują z mocy prawa). "
                "Żaden doradca ani prawnik nie może tego zmienić, ale nie zniechęcaj się – mamy świetne wiadomości! 🌟\n\n"
                "Rynek w Tajlandii oferuje fantastyczne, w 100% legalne i bezpieczne ścieżki inwestycyjne, z których z powodzeniem korzystają tysiące inwestorów:\n\n"
                "1. **Foreign Freehold w Kondominium:** 100% pełnej, wieczystej własności lokalu wpisanej bezpośrednio na Twoje nazwisko w księdze wieczystej (w ramach ustawowej 49% kwoty dla cudzoziemców w budynku).\n"
                "2. **Luksusowe Wille (Własność Budynku Freehold + Rejestrowany Leasehold Gruntu):** Jesteś pełnoprawnym właścicielem samego budynku na własność (Freehold), a grunt zabezpiecza oficjalna, 30-letnia rejestrowana umowa dzierżawy (z odnowieniami 30+30+30 lat, łącznie do 90 lat), wpisywana urzędowo na odwrocie aktu własności Chanote w państwowym Land Department.\n"
                "3. **Spółka Prawa Tajskiego:** Nabycie gruntu przez prawidłowo ustrukturyzowaną spółkę komercyjną pod nadzorem licencjonowanej kancelarii prawnej.\n\n"
                "W kwestii podatków: standardowe opłaty w Land Department obejmują 2% opłaty transferowej, podatek u źródła (withholding tax) oraz opłatę skarbową (0,5%) lub podatek od działalności (3,3%) – zwyczajowo dzielone między kupującego a dewelopera/sprzedawcę.\n\n"
                "Z ogromną motywacją pomożemy Ci przejść przez cały proces w pełni bezpiecznie i bez stresu! "
                "Czy chciałbyś zobaczyć prestiżowe apartamenty Foreign Freehold czy prywatne wille z basenem na Phuket lub Koh Samui?"
            )
        else:
            return (
                "Hello! On this question, Thai law is absolute and uncompromising: under Section 86 of the Thai Land Code, "
                "the factual answer is firmly **NO** – foreign nationals cannot directly purchase or own freehold land in their personal name, "
                "and real estate transactions in Thailand are **never 100% tax-free** (government transfer fees and taxes always apply by law). "
                "No amount of pressure or informal advice can bypass Thai law, but don't let that discourage you at all! 🌟\n\n"
                "There are proven, completely legal, and highly secure pathways that thousands of international buyers use every year:\n\n"
                "1. **Foreign Freehold Condominiums:** You hold 100% direct, perpetual freehold ownership in your individual name (within the 49% foreign quota of the condominium development).\n"
                "2. **Luxury Villas (Freehold Building + Registered Leasehold Land):** You own the physical villa building in absolute Freehold, paired with a registered, legally protected 30-year renewable leasehold (30+30+30 years, totaling 90 years) recorded directly on the Chanote title deed at the government Land Department.\n"
                "3. **Compliant Thai Corporate Structures:** Established with independent, licensed Thai legal counsel.\n\n"
                "Regarding taxes: official government Land Department fees and taxes include a 2% transfer fee, withholding tax, and stamp duty (0.5%) or special business tax (3.3%), which are standard by law and typically shared between buyer and seller.\n\n"
                "We are excited and ready to help you find your dream luxury property with 100% legal security! "
                "Would you like to explore verified Foreign Freehold condominiums or luxury pool villas in Phuket or Koh Samui?"
            )

    @classmethod
    def sanitize_response(cls, user_message: str, response_text: str, language: str = "en") -> str:
        """
        Intercepts model responses to ensure anti-pressure resilience and legal compliance.
        If the model conceded to user pressure or generated an illegal claim, sanitizes with the
        compliant, friendly, and motivated response.
        """
        is_illegal, _ = cls.check_illegal_claims(response_text)
        if is_illegal:
            logger.warning("ThaiPropertyLegalGuardrail: Illegal property claim detected. Sanitizing with compliant response.")
            return cls.generate_compliant_friendly_response(language)

        # Check if the user applied adversarial pressure on land or tax and the model yielded
        u_low = (user_message or "").lower()
        resp_low = (response_text or "").lower()

        asked_land = any(k in u_low for k in cls.PRESSURE_LAND_KEYWORDS) or bool(re.search(r"\bown\s+(?:the\s+)?land\b|\bland\s+(?:100%|freehold)\b|\bfreehold\s+land\b", u_low))
        asked_tax = any(k in u_low for k in cls.PRESSURE_TAX_KEYWORDS) or bool(re.search(r"\btax[\s-]free\b|\bno\s+tax(?:es)?\b", u_low))
        pressured = any(re.search(p, u_low) for p in cls.LEADING_PRESSURE_PATTERNS)

        if (asked_land or asked_tax) and pressured:
            # Under pressure, the model MUST NOT start with or confirm "Yes" or agree to illegal premises
            has_yielding_yes = any(re.search(r'\b' + re.escape(w) + r'\b', resp_low[:100]) for w in [
                "yes", "yes you can", "correct", "you are right", "absolutely yes",
                "tak", "tak, możesz", "tak, mozesz", "masz rację", "masz racje",
                "ใช่", "ถูกต้อง", "ใช่ครับ"
            ])
            if has_yielding_yes:
                logger.warning("ThaiPropertyLegalGuardrail: Model yielded to user pressure. Overriding with friendly motivated NO.")
                return cls.generate_compliant_friendly_response(language)

        return response_text
