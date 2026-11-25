#!/bin/bash

# ================================
# 1. 核心参数配置
# ================================
CONCURRENCY=100
PROXY_URL="http://localhost:6006" # FastAPI 代理服务地址
OUTPUT_DIR="testAsyncAudios"      # 异步测试音频输出文件夹
source /etc/network_turbo
# 自动创建目标文件夹
mkdir -p "$OUTPUT_DIR"

# 关闭作业控制的后台提示
set +m 

# ================================
# 2. 准备请求负载
# ================================
read -r -d '' MY_MULTILINE_TEXT << 'TEXT_EOF'
第一行：SGLang-Omni 真不错！
第二行：遇到 400 报错不要慌，看看是不是少了字段。
第三行："这里还可以随意使用双引号" 并且完全不会报错！
TEXT_EOF

# 转义 JSON 特殊字符
ESCAPED_TEXT="${MY_MULTILINE_TEXT//\\/\\\\}"
ESCAPED_TEXT="${ESCAPED_TEXT//\"/\\\"}"
ESCAPED_TEXT="${ESCAPED_TEXT//$'\n'/\\n}"
ESCAPED_TEXT="${ESCAPED_TEXT//$'\r'/}"

echo "⏳ 正在下载并转换参考音频..."

# 开启管道错误捕获：只要管道前方的 curl 失败，整个命令就会返回失败
set -o pipefail 

BASE64_AUDIO=$(curl -sSLf "https://huggingface.co/datasets/zhaochenyang20/seed-tts-eval-mini/resolve/main/en/prompt-wavs/common_voice_en_10119832.wav" | base64 -w 0) || {
    echo "❌ 下载参考音频失败！请检查网络或代理设置。"
    set +o pipefail # 恢复默认设置
    exit 1
}

set +o pipefail # 成功了也要恢复默认设置

if [ -z "$BASE64_AUDIO" ]; then
    echo "❌ Base64 数据为空！"
    exit 1
fi

TMP_JSON=$(mktemp)
cat <<EOF > "$TMP_JSON"
{
  "input": "${ESCAPED_TEXT}",
  "max_new_tokens": 30000,
  "references": [{
    "audio_path": "data:audio/wav;base64,${BASE64_AUDIO}",
    "text": "We asked over twenty different people, and they all said it was his."
  }]
}
EOF

# ================================
# 3. 执行异步并发请求并状态轮询
# ================================
echo "🚀 准备就绪！正在同时发起 ${CONCURRENCY} 个【异步】生成请求..."
echo "📂 所有产出音频将保存至: ./${OUTPUT_DIR}/"
echo "---------------------------------------------------"

STATS_LOG=$(mktemp) # 用于记录各个任务的耗时和状态码

for (( i=1; i<=CONCURRENCY; i++ )); do
  (
    START_TIME=$(date +%s)
    
    # Step 1: 提交任务，获取 task_id
    INIT_RESP=$(curl -s -X POST "${PROXY_URL}/v1/audio/async/speech?task_id=test_${i}" \
      -H "Content-Type: application/json" \
      -d @"$TMP_JSON")
    
    TASK_ID=$(echo "$INIT_RESP" | grep -o '"task_id":"[^"]*' | grep -o '[^"]*$')
    
    if [ -z "$TASK_ID" ]; then
        echo "❌ 任务 [$i] 提交失败 | 响应: ${INIT_RESP}"
        echo "500 0" >> "$STATS_LOG"
        exit 1
    fi
    
    # Step 2: 轮询状态直到成功或失败
    while true; do
        sleep 5 # 每 5 秒轮询一次
        
        STATUS_RESP=$(curl -s -X GET "${PROXY_URL}/v1/audio/async/status/${TASK_ID}")
        STATUS=$(echo "$STATUS_RESP" | grep -o '"status":"[^"]*' | grep -o '[^"]*$')
        
        if [ "$STATUS" = "success" ]; then
            DOWNLOAD_URL=$(echo "$STATUS_RESP" | grep -o '"result":"[^"]*' | grep -o '[^"]*$')
            
            # Step 3: 下载生成的真正 wav 音频文件，保存到指定文件夹中
            curl -s -o "${OUTPUT_DIR}/output_long_${i}.wav" "$DOWNLOAD_URL"
            
            END_TIME=$(date +%s)
            ELAPSED=$((END_TIME - START_TIME))
            echo "200 $ELAPSED" >> "$STATS_LOG"
            echo "✅ 任务 [$i] 完成 | 状态码: 200 | 总历时: ${ELAPSED} 秒"
            break
            
        elif [ "$STATUS" = "failed" ] || [ -z "$STATUS" ]; then
            END_TIME=$(date +%s)
            ELAPSED=$((END_TIME - START_TIME))
            echo "500 $ELAPSED" >> "$STATS_LOG"
            echo "❌ 任务 [$i] 失败 | 后端报错 | 历时: ${ELAPSED} 秒"
            break
        fi
    done
  ) &
done

# 等待所有后台轮询和下载进程完成
wait

# ================================
# 4. 统计与分析 (使用 awk 和 ffprobe)
# ================================
echo "---------------------------------------------------"
echo "📊 任务执行完毕，正在生成统计报告..."

SUCCESS_COUNT=$(grep -c "^200 " "$STATS_LOG")
TOTAL_TIME=$(awk '{sum+=$2} END {print sum}' "$STATS_LOG")
AVG_TIME=$(awk '{sum+=$2} END {if(NR>0) printf "%.2f", sum/NR}' "$STATS_LOG")

TOTAL_AUDIO_DURATION=0
if command -v ffprobe &> /dev/null; then
    for (( i=1; i<=CONCURRENCY; i++ )); do
        # 从指定的文件夹中读取文件进行时长统计
        FILE="${OUTPUT_DIR}/output_long_${i}.wav"
        if [ -f "$FILE" ]; then
            DURATION=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$FILE" 2>/dev/null)
            if [ -n "$DURATION" ]; then
                TOTAL_AUDIO_DURATION=$(awk "BEGIN {print $TOTAL_AUDIO_DURATION + $DURATION}")
            fi
        fi
    done
    FORMATTED_AUDIO_DURATION=$(awk "BEGIN {printf \"%.2f\", $TOTAL_AUDIO_DURATION}")
    AUDIO_STAT_MSG="${FORMATTED_AUDIO_DURATION} 秒"
else
    AUDIO_STAT_MSG="未安装 ffprobe，无法统计 (请安装 ffmpeg)"
fi

# ================================
# 5. 打印最终报表
# ================================
echo ""
echo "================ 📈 并发测试报告 =================="
echo "总并发数     : ${CONCURRENCY}"
echo "成功任务数   : ${SUCCESS_COUNT} / ${CONCURRENCY}"
echo "平均任务全长 : ${AVG_TIME} 秒/请求 (含排队轮询时间)"
echo "产出音频总长 : ${AUDIO_STAT_MSG}"
if [[ $(echo "$TOTAL_AUDIO_DURATION > 0" | awk '{print ($1 > 0)}') -eq 1 && $(echo "$TOTAL_TIME > 0" | awk '{print ($1 > 0)}') -eq 1 ]]; then
    RTF=$(awk "BEGIN {printf \"%.3f\", $TOTAL_TIME / $TOTAL_AUDIO_DURATION}")
    echo "系统 RTF 估算: ${RTF}"
fi
echo "==================================================="

# 清理进程临时文件（保留音频文件夹）
rm -f "$TMP_JSON" "$STATS_LOG"