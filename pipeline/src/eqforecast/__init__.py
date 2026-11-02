"""eqforecast：地震「警報式」預報的回測框架（B3）。

模組分工（資料由上往下流動）：
    config      讀取 JSON 設定
    grid        研究區網格與網格編號
    decluster   Gardner–Knopoff／Uhrhammer 視窗法去群聚
    targets     發布時間點與「網格 × 預報窗」的目標標籤
    splits      時間切分（含 purge／embargo）與洩漏檢查
    features    只用地震目錄的特徵，以及外部前兆特徵的掛載介面
    models      隨機、相對強度（RI）、邏輯斯迴歸
    metrics     POD／FAR／CSI、Molchan 曲線、顯著性檢定
"""
