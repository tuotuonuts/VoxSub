"""voxsub.translate.tokenizer —— 手写 SentencePiece Unigram 分词器 (零第三方依赖)。

Xenova 导出的 OPUS-MT 模型只在 ``tokenizer.json`` 里嵌了一份
``Unigram`` (即 sentencepiece) 词表, 不附带独立的 .model 文件。为
避免引入 transformers / sentencepiece 重型依赖 (任务说明: 手解可行则
不走 ctranslate2 / 不引 transformers), 这里用纯标准库实现:

1. Pre-tokenizer: WhitespaceSplit → Metaspace (空格 换 U+2581 '▁', 每片加前缀)
2. Normalizer: 该模型为 Precompiled(null charsmap) → 恒等, 跳过
3. 编码  : Unigram Viterbi —— 在词表子串上做动态规划, 取对数得分和最大的切分
4. 后处理: 单句末尾追加 </s> (post_processor TemplateProcessing)
5. 解码  : Metaspace decode (▁ → 空格, 去首部空格)

仅依赖 stdlib (json/unicodedata)。所有分词函数接受/返回 str 与 id 列表。
"""
from __future__ import annotations

import json
from pathlib import Path

NEG_INF = float("-inf")
#: Metaspace 替换符 (sentencepiece 的拼接空格符)
SPACE_MARK = "\u2581"


class UnigramTokenizer:
    """从 HF tokenizer.json 加载的 Unigram 分词器 (单句/批外通用)。"""

    def __init__(self, vocab: list[tuple[str, float]], unk_id: int):
        # vocab: (piece, logscore) 列表, 已在加载时按 id 排序
        self.unk_id = unk_id
        self.piece_by_id: dict[int, str] = {}
        self._pieces: dict[str, tuple[int, float]] = {}   # piece -> (id, score)
        self._max_piece_len = 1
        for pid, (piece, score) in enumerate(vocab):
            self.piece_by_id[pid] = piece
            self._pieces[piece] = (pid, score)
            n = len(piece)
            if n > self._max_piece_len:
                self._max_piece_len = n

    # ------------------------------------------------------------------
    @classmethod
    def from_file(cls, tokenizer_json: str | Path) -> "UnigramTokenizer":
        data = json.loads(Path(tokenizer_json).read_text(encoding="utf-8"))
        model = data["model"]
        if model["type"] != "Unigram":
            raise ValueError(f"仅支持 Unigram 词表, 实际为 {model['type']}")
        # vocab 已是 [(piece, score)] 且按 id 升序 (tokenizer.json 约定)
        vocab = model["vocab"]
        unk_id = model.get("unk_id", 0)
        return cls(vocab, unk_id)

    # ------------------------------------------------------------------
    # Pre-tokenization: WhitespaceSplit → Metaspace
    # ------------------------------------------------------------------
    def _pretokenize(self, text: str) -> list[str]:
        """先按空白切成 whitespace token, 再给每个 token 加 Metaspace 前缀。"""
        out = []
        for tok in text.split():
            out.append(SPACE_MARK + tok)
        if not out:            # 文本全空白或为空 → 空分词结果
            return []
        return out

    # ------------------------------------------------------------------
    # Unigram Viterbi 编码
    # ------------------------------------------------------------------
    def _encode_subtoken(self, s: str) -> list[int]:
        """对单个 Metaspace token (不含内部空白) 做 Unigram Viterbi 切分。"""
        L = len(s)
        if L == 0:
            return []
        MAXL = min(self._max_piece_len, L)
        # best_score[b] = 到字符边界 b 的最佳对数得分
        # best_id[b]    = 进入边界 b 的那一片的 (id, 起始边界)
        best_score = [NEG_INF] * (L + 1)
        best_id = [-1] * (L + 1)
        best_prev = [-1] * (L + 1)
        best_score[0] = 0.0

        for i in range(L):
            if best_score[i] == NEG_INF:
                continue  # 不可达边界, 跳过 (后方兜底处理)
            base = best_score[i]
            # 尝试所有从 i 起始、且在词表中的子串
            any_match = False
            for plen in range(1, MAXL + 1):
                if i + plen > L:
                    break
                hit = self._pieces.get(s[i:i + plen])
                if hit is None:
                    continue
                any_match = True
                pid, score = hit
                j = i + plen
                cand = base + score
                if cand > best_score[j]:
                    best_score[j] = cand
                    best_id[j] = pid
                    best_prev[j] = i
            # 从 i 起一个词表子串都没有 → 单字符 unknown 兜底, 保证 L 可达
            if not any_match and best_score[i + 1] == NEG_INF:
                best_score[i + 1] = base
                best_id[i + 1] = self.unk_id
                best_prev[i + 1] = i

        # 反向回溯; 极罕见的历史空洞用单字符 unk 兜底保证终止
        ids: list[int] = []
        idx = L
        guard = L + 8
        while idx > 0 and guard > 0:
            guard -= 1
            if best_id[idx] == -1 or best_prev[idx] == -1:
                ids.append(self.unk_id)
                idx -= 1
                continue
            ids.append(best_id[idx])
            idx = best_prev[idx]
        ids.reverse()
        return ids

    def encode(self, text: str, add_special: bool = True) -> list[int]:
        """文本 → id 列表。add_special=True 时追加 </s> (post_processor 契约)。

        注: 每个 Metaspace 片段的编码结果顺序拼接; 尾部追加 </s> (id 0)。
        """
        ids: list[int] = []
        for tok in self._pretokenize(text):
            ids.extend(self._encode_subtoken(tok))
        if add_special:
            ids.append(0)  # </s>
        return ids

    # ------------------------------------------------------------------
    def decode(self, ids: list[int]) -> str:
        """id 列表 → 文本 (Metaspace decode: ▁ → 空格, 去首部空格)。

        无 tokenizer.json 的 id->piece 映射时退回 ids==-1 / 越界为 <unk>。
        """
        pieces = []
        for pid in ids:
            if pid < 0 or pid >= len(self.piece_by_id):
                continue
            piece = self.piece_by_id[pid]
            if piece in ("</s>", "<pad>", "<unk>"):
                continue
            pieces.append(piece)
        text = "".join(pieces)
        text = text.replace(SPACE_MARK, " ")
        return text.strip()

    def id_to_piece(self, pid: int) -> str:
        return self.piece_by_id.get(pid, "<unk>")
