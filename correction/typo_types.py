"""错别字的机械分类：类型只从改动本身判定，不问模型。

模型自称"这是错别字"不可信——实测它会把自己编出来的电话号码（020-3812-ABCD →
020-3812-1234）标成"#p0 错别字"。但"这处改动属于哪种错误"是从编辑算得出来的确定事实：

    同音字    单字替换，原字与改后字读音相同（声调可同可异）：中过→中国、科技元→科技园
    近音字    单字替换，读音只差平翘舌或鼻韵尾（zh/z、sh/s、前后鼻音不分）：
              搜→收、事→四、一→银、身→生
    字母数字  单字替换，一侧是 ASCII 字母、另一侧是它的形近数字：O→0、l→1
    同字连写  同一个字连写了两个以上，删到只剩一个：款款款款款→款、卖方方仓库→卖方仓库
    多打的字  删掉一个汉字，它与前后相邻的字都不同：开户航行→开户行、卖你方→卖方
    漏写的字  补上一个汉字，它和左右紧邻的字都不同，且它和左邻或右邻组成的两个字在全文
              别处出现过：邮编码→邮政编码（「邮政」在别处出现过）；左右紧邻的都要是汉字

改动块只由"字级"动作搭出来：换 1 个字、删 1 个字、补 1 个字、把一段连写收成 1 个字。一句话
里错好几处、同一段里写好几种动作都可以，只要每个动作都只落在单个字上。删一串互不相同的字
（那是在改内容，不是在纠错）、一次补不止一个字，都不执行。

删字的分界线在"连写几个"：

    连写 ≥3 个（款款款款）  删到剩 1 个——正式文本里不存在三个同字连写；
    连写 2 个（卖方方仓库）  再看右边那个字：这个字和它组成的两个字要是全文别处也出现过
                            （增值税税率、开户行行号、符合合同、预付款前），说明分属两个
                            词，不删；别处没出现过（方仓）才是多打了一个，删；
    只删 1 个字且与前后都不同（航行→行）  删——多打了一个不相干的字，读起来是断的。

删字里"多打的字"这一类证据最弱（没有读音证据，靠的是"这个字和左右都不搭"），所以只对汉字
开放：数字、字母连写（5000、1000、aa）本来就是这样写的，一个都不删。

"漏写的字"没有读音证据（漏掉的字本来就不在原文里），是唯一一类要拿全文当语料的：补出来的
两个字得是本文档在用的搭配才算数。实测（客户合同）：邮编码→邮政编码 落地（「邮政」别处出现
过），而「达成利益默契签订」想补个「或」——那是表述问题、不是可机械验证的错别字——落不了地。

实测（36,809 字客户 PDF）：全文有两字连写 24 处，其中 23 处是"分属两个词"的正常写法
（增值税税率、符合合同、合理理由…），靠"别处也出现过"这一条全部拦住；只有「卖方方仓库」
一处右边那个字组不出词，按同字连写收掉一个。

反向的例子也在同一批实测里：13,276 字客户合同里模型提了删「款」（及尾款款前），右边那个字
组出来的是「款前」——同一份文件里「预付款前」出现过两次，按"分属两个词"挡了回去，一个字没碰。

"左右紧邻的都要是汉字"这条也是实测逼出来的：PDF 是按行抽出来的，很多单元从词中间开始，模型
看到「方应在收到买方的通知后7日内免费补齐短缺…」就要补「卖」（「卖方」全文到处都是），而上一
行正好以「卖」结尾——那只是换行把"卖方"切开了，一个字都不缺；一次跑下来它在 6 个这样的行首各
补了一个字，全是假的。判据只认汉字也是因为这个：整篇是把单元用 \n 拼成一整段交给模型的，
行首那个 \n 会被当成"左邻字"混过去。
"""

from __future__ import annotations

import re

from pypinyin import Style, lazy_pinyin

TONGYIN = "同音字"
JINYIN = "近音字"
ZIMU = "字母数字"
LIANXIE = "同字连写"
DUOYU = "多打的字"
LOUXIE = "漏写的字"
#: 会被执行的错误类型。这些之外的一律不执行。
KINDS = (TONGYIN, JINYIN, ZIMU, LIANXIE, DUOYU, LOUXIE)

#: 同一字连写到几个就一律认定是删多了：3 个。正好 2 个的要再看右边那个字（见 classify_delete）。
LIANXIE_MIN = 3

#: 韵尾 n / ng。前后鼻音不分是最高频的一类错法（一/银、身/生、民/明）。
NASAL_TAIL = re.compile(r"ng?$")

#: 平翘舌：zh/z、ch/c、sh/s 不分（搜 sōu / 收 shōu、事 shì / 四 sì）。
RETROFLEX = re.compile(r"^(zh|ch|sh)")

# 形近的字母/数字对：把字母临摹成数字（或反过来）是常见错法。不能放宽成"任意字母换任意
# 数字"——那样"编号 I 改成 1"之类的改写也会被当成错别字放行。
CONFUSABLE = frozenset({
    ("O", "0"), ("0", "O"), ("o", "0"), ("0", "o"),
    ("l", "1"), ("1", "l"), ("I", "1"), ("1", "I"), ("i", "1"), ("1", "i"),
    ("Z", "2"), ("2", "Z"), ("z", "2"), ("2", "z"),
    ("S", "5"), ("5", "S"), ("s", "5"), ("5", "s"),
    ("B", "8"), ("8", "B"), ("G", "6"), ("6", "G"),
    ("D", "0"), ("0", "D"), ("q", "9"), ("9", "q"), ("g", "9"), ("9", "g"),
})


def pinyin_of(char: str, style: Style = Style.NORMAL) -> str:
    """取单字读音；非汉字（数字、字母、标点）返回空串。"""
    parts = lazy_pinyin(char, style=style, errors="ignore")
    return parts[0] if parts else ""


def is_cjk(char: str) -> bool:
    """是不是汉字（含扩展 A 区）。"""
    return "\u3400" <= char <= "\u4dbf" or "\u4e00" <= char <= "\u9fff"


def _near_stem(pinyin: str) -> str:
    """归一化到"听着差不多"的音：先并平翘舌，再去鼻韵尾，用来判"近音"。

    平翘舌不分（搜 sōu / 收 shōu、事 shì / 四 sì）和前后鼻音不分（一 yī / 银 yín、
    身 shēn / 生 shēng）是最常见的两类错法，两类都归到同一个音上。

    "a / e / o" 这种单韵母音节不去鼻韵尾，否则「安/啊」「恩/饿」也会被当成近音。
    """
    stem = RETROFLEX.sub(lambda match: match.group(1)[0], pinyin)
    trimmed = NASAL_TAIL.sub("", stem)
    return trimmed if len(trimmed) >= 2 else stem


def classify(tag: str, original: str, correct: str) -> str | None:
    """判定一处单字改动属于哪类错别字。

    返回 None 表示"没法验证这是错别字"，调用方据此拒绝这处改动。
    """
    if tag == "replace" and len(original) == 1 and len(correct) == 1:
        if (original, correct) in CONFUSABLE:
            return ZIMU
        plain = pinyin_of(original)
        other = pinyin_of(correct)
        if plain and plain == other:
            return TONGYIN
        if plain and other and _near_stem(plain) == _near_stem(other):
            return JINYIN
    return None


def _pair_attested_elsewhere(corpus: str, pair: str, exclude_at: int, here_after: str) -> bool:
    """这个两字搭配在别处算不算「文档里在用的写法」。

    出现 ≥2 次就算。只出现 1 次时还要看那一次的用法：后面跟的字和这里一样，说明是同一种切法
    切出来的假词——「买方仓库」里的「方仓」和「卖方方仓库」里的「方仓」是同一个切法，不算证据；
    后面跟的字不同才算——「开发目标，」和「项目目标：」是两种用法，说明「目标」是真词。
    exclude_at 是待改这一处里 pair 的起点，自己这一次不算证据。
    """
    total = 0
    varied = False
    index = corpus.find(pair)
    while index != -1:
        if index != exclude_at:
            total += 1
            after = corpus[index + len(pair)] if index + len(pair) < len(corpus) else ""
            if after != here_after:
                varied = True
        index = corpus.find(pair, index + 1)
    return total >= 2 or varied


def classify_delete(
    removed: str, run_len: int, following: str = "", corpus: str = "", start: int = -1
) -> str | None:
    """判定一处删字属于哪类错别字。

    removed 是被删掉的那串字（收连写时是整段，例「款款款款」）；
    run_len 是它在原文里的连写长度（同一个字连续相同的个数）；
    following 是这段连写右边紧邻的那个字；corpus 是 000.txt 全文。
    start 是这段连写在 corpus 里的起点（用来把待改这一处从证据里排除掉）。
    返回 None 表示"没法验证这是多打的字"，调用方据此拒绝这处改动。

    连写 3 个以上一律删（正式文本里不存在三个同字连写）；正好 2 个时多看一眼右边那个字——
    它和右边那个字组成的两个字要是算得上"文档里在用的写法"（见 _pair_attested_elsewhere：
    增值税税率、开户行行号、符合合同），说明这两个字分属两个词，不动；只在同一种切法里出现过
    （「买方仓库」里的「方仓」）或者压根没出现过（设备备妥里的「备妥」），才是多打了一个。
    """
    if not removed or not all(is_cjk(char) for char in removed):
        return None
    if len(set(removed)) != 1:
        return None
    if run_len >= LIANXIE_MIN:
        return LIANXIE
    if run_len == 2 and len(removed) == 1:
        pair = removed + following
        pair_at = start + run_len - 1 if start >= 0 else -1
        after_at = pair_at + len(pair)
        here_after = corpus[after_at] if 0 <= after_at < len(corpus) else ""
        if following and _pair_attested_elsewhere(corpus, pair, pair_at, here_after):
            return None
        return LIANXIE
    if run_len == 1 and len(removed) == 1:
        return DUOYU
    return None


#: 「漏写的字」要求补出来的两个字在全文别处至少出现过这么多次。拿 000.txt 自己当语料：
#: 真漏字（邮编码→邮政编码）补出来的「邮政」别处也出现；模型现编的搭配别处不会出现。
INSERT_PAIR_MIN = 1


def classify_insert(inserted: str, left: str, right: str, corpus: str) -> str | None:
    """判定一处补字属于哪类错别字（漏写了一个字）。

    inserted 是补进去的那个字；left / right 是它在 000.txt 里左右紧邻的字（没有就传空串）；
    corpus 是 000.txt 全文。返回 None 表示"没法验证是漏字"，调用方据此拒绝。

    这一类和多打的字一样没有读音证据——漏掉的那个字本来就不在原文里，读不出音——所以换用
    文档自己当语料：补出来的两个字得是本文档在用的搭配，才算数。

    左右紧邻的必须是汉字：单元（行 / 段落）的开头和结尾、换行、标点旁边都不给补字。那里的
    "邻字"不归这一行管，很可能只是换行把词切开了（实测：模型看到「术服务应符合合同的约定」就
    补「技」，而上一行正好以「技」结尾），补上去等于凭空多一个字。
    """
    if len(inserted) != 1 or not is_cjk(inserted):
        return None
    if not is_cjk(left) or not is_cjk(right):
        return None
    if inserted == left or inserted == right:
        return None
    if len(left + inserted) == 2 and corpus.count(left + inserted) >= INSERT_PAIR_MIN:
        return LOUXIE
    if len(inserted + right) == 2 and corpus.count(inserted + right) >= INSERT_PAIR_MIN:
        return LOUXIE
    return None
