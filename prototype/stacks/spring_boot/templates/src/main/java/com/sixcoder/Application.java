// Spring Boot 入口（**由引擎生成，不要改**）。
//
// 与 Web 入口同一原则：入口只做装配（@SpringBootApplication 触发组件扫描），
// 业务控制器放在 com.sixcoder 包下会被自动发现。
package com.sixcoder;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class Application {
    public static void main(String[] args) {
        SpringApplication.run(Application.class, args);
    }
}
