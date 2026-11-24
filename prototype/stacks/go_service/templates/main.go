// Go 服务入口（**由引擎生成，不要改**）。
//
// 为什么不让你写：业务代码是分模块长的（internal/<域>/<域>.go），
// 每加一个模块就重写 main 的话，之前的初始化会被抹掉。
//
// 你只需要在 internal/ 下的包里写导出函数；main 只负责把服务起起来。
package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
)

func health(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]bool{"ok": true})
}

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", health)

	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	fmt.Println("listening on " + port)
	if err := http.ListenAndServe(":"+port, mux); err != nil {
		fmt.Fprintln(os.Stderr, "server error: "+err.Error())
		os.Exit(1)
	}
}
